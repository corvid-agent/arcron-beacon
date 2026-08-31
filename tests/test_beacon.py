"""Unit tests for the Beacon contract using algorand-python-testing.

These mock-chain tests cover authorization, the plan/reveal state machine,
the configurable plan delay, and fail-soft behaviour. Per
docs/integrating.md in CorvidLabs/arcron, mocked tests cannot prove keeper
integration (inner-call execution, MBR); that proof belongs to a
LocalNet/TestNet e2e at deploy time.

Note: the mock chain's Block.blk_seed returns itob(seed_int); on a real
chain it is a 32-byte VRF seed. The contract stores whatever the AVM
returns either way.
"""

import pytest
from algopy import UInt64
from algopy.op import itob
from algopy_testing import AlgopyTestContext, algopy_testing_context

from smart_contracts.beacon.contract import (
    DELAY_ROUNDS,
    MAX_DELAY,
    STALE_MARGIN,
    Beacon,
)


@pytest.fixture()
def context() -> AlgopyTestContext:
    with algopy_testing_context() as ctx:
        yield ctx


def _deploy(context: AlgopyTestContext):
    contract = Beacon()
    contract.create()
    keeper = context.any.application()
    contract.set_keeper(keeper)
    return contract, keeper


def _as(context: AlgopyTestContext, contract: Beacon, sender):
    """Run inside a group whose active txn sender is `sender`."""
    return context.txn.create_group(
        [
            context.any.txn.application_call(
                app_id=context.ledger.get_app(contract.__app_id__),
                sender=sender,
            )
        ]
    )


def test_set_keeper_creator_only(context: AlgopyTestContext) -> None:
    contract = Beacon()
    contract.create()
    keeper = context.any.application()
    stranger = context.any.account()
    with _as(context, contract, stranger):
        with pytest.raises(AssertionError, match="Only the creator"):
            contract.set_keeper(keeper)
    contract.set_keeper(keeper)
    assert contract.keeper_app.value == keeper.id


def test_set_keeper_one_time(context: AlgopyTestContext) -> None:
    contract, keeper = _deploy(context)
    with pytest.raises(AssertionError, match="Keeper already set"):
        contract.set_keeper(keeper)


def test_set_delay_creator_only(context: AlgopyTestContext) -> None:
    contract = Beacon()
    contract.create()
    stranger = context.any.account()
    with _as(context, contract, stranger):
        with pytest.raises(AssertionError, match="Only the creator"):
            contract.set_delay(UInt64(100))
    contract.set_delay(UInt64(100))
    assert contract.delay_rounds.value == 100


def test_set_delay_bounds(context: AlgopyTestContext) -> None:
    # Zero and anything above MAX_DELAY are rejected; the setter is
    # one-time, so each bound check needs a fresh contract.
    contract = Beacon()
    contract.create()
    with pytest.raises(AssertionError, match="at least 1 round"):
        contract.set_delay(UInt64(0))
    contract.set_delay(UInt64(1))
    assert contract.delay_rounds.value == 1

    contract2 = Beacon()
    contract2.create()
    with pytest.raises(AssertionError, match="exceeds the blk_seed window"):
        contract2.set_delay(UInt64(MAX_DELAY + 1))
    contract2.set_delay(UInt64(MAX_DELAY))
    assert contract2.delay_rounds.value == MAX_DELAY


def test_set_delay_one_time(context: AlgopyTestContext) -> None:
    contract = Beacon()
    contract.create()
    contract.set_delay(UInt64(100))
    with pytest.raises(AssertionError, match="Delay already set"):
        contract.set_delay(UInt64(200))
    assert contract.delay_rounds.value == 100


def test_publish_requires_keeper(context: AlgopyTestContext) -> None:
    contract, _ = _deploy(context)
    with pytest.raises(AssertionError, match="Only the keeper app"):
        contract.publish()


def test_plan_then_reveal(context: AlgopyTestContext) -> None:
    contract, keeper = _deploy(context)
    with _as(context, contract, keeper.address):
        context.ledger.patch_global_fields(round=1000)
        # Phase 1: plan — commits to now + DELAY_ROUNDS, returns 0.
        assert contract.publish() == 0
        target = 1000 + DELAY_ROUNDS
        assert contract.target_round.value == target

        # Too early: fail-soft return, target unchanged, nothing revealed.
        assert contract.publish() == 0
        assert contract.target_round.value == target
        assert contract.reveals.value == 0

        # Advance past the target; reveal publishes that round's seed.
        context.ledger.set_block(target, seed=42, timestamp=1)
        context.ledger.patch_global_fields(round=target + 1)
        assert contract.publish() == target
        assert contract.revealed_round.value == target
        assert contract.revealed_seed.value == itob(42)
        assert contract.reveals.value == 1
        # Commitment cleared; the next call plans again.
        assert contract.target_round.value == 0
        assert contract.publish() == 0
        assert contract.target_round.value == target + 1 + DELAY_ROUNDS


@pytest.mark.parametrize(
    ("delay", "cadence"),
    [
        # Default delay at its documented cadence ceiling: 20 + 900.
        (None, DELAY_ROUNDS + STALE_MARGIN),
        # Max configured delay at its documented cadence ceiling: 800 + 900
        # (~80 min at 2.8 s/round) — the slowest keeper cadence supported.
        (MAX_DELAY, MAX_DELAY + STALE_MARGIN),
    ],
)
def test_full_cycle_at_documented_cadence(
    context: AlgopyTestContext, delay: int | None, cadence: int
) -> None:
    """Drive repeated plan->reveal cycles at the documented maximum cadence.

    Each keeper call lands exactly `cadence` rounds after the previous one;
    the reveal attempt arrives `cadence - delay` rounds after the target,
    which must be <= STALE_MARGIN. This is the failure class from the
    pre-deploy audit: a cadence that violates the invariant re-plans
    forever, so the suite pins the boundary where it still works.
    """
    contract, keeper = _deploy(context)
    if delay is not None:
        contract.set_delay(UInt64(delay))
    effective_delay = delay if delay is not None else DELAY_ROUNDS
    with _as(context, contract, keeper.address):
        round_ = 10_000
        context.ledger.patch_global_fields(round=round_)
        for cycle in range(1, 4):
            # Keeper tick 1 of the cycle: PLAN.
            assert contract.publish() == 0
            target = round_ + effective_delay
            assert contract.target_round.value == target
            # Advance exactly one cadence; the reveal lands at the edge of
            # the seed window (cadence - delay == STALE_MARGIN).
            round_ += cadence
            context.ledger.set_block(target, seed=cycle, timestamp=1)
            context.ledger.patch_global_fields(round=round_)
            # Keeper tick 2 of the cycle: REVEAL, not a re-plan.
            assert contract.publish() == target
            assert contract.revealed_round.value == target
            assert contract.revealed_seed.value == itob(cycle)
            assert contract.reveals.value == cycle
            assert contract.target_round.value == 0


def test_replan_only_when_seed_window_expired(context: AlgopyTestContext) -> None:
    """The stale-replan path fires only past the window, never inside it.

    now - target == STALE_MARGIN must still REVEAL (the seed is readable
    for 1000 rounds back); only now - target > STALE_MARGIN re-plans.
    """
    contract, keeper = _deploy(context)
    with _as(context, contract, keeper.address):
        context.ledger.patch_global_fields(round=1000)
        contract.publish()
        target = 1000 + DELAY_ROUNDS

        # Boundary, inside the window: reveals rather than re-plans.
        context.ledger.set_block(target, seed=7, timestamp=1)
        context.ledger.patch_global_fields(round=target + STALE_MARGIN)
        assert contract.publish() == target
        assert contract.reveals.value == 1
        assert contract.target_round.value == 0

        # One round past the boundary: the seed is unreadable, re-plan.
        context.ledger.patch_global_fields(round=2000)
        assert contract.publish() == 0  # plans
        target2 = 2000 + DELAY_ROUNDS
        context.ledger.patch_global_fields(round=target2 + STALE_MARGIN + 1)
        assert contract.publish() == 0  # re-plans, does not raise
        assert contract.reveals.value == 1  # no phantom reveal
        assert contract.target_round.value == (
            target2 + STALE_MARGIN + 1 + DELAY_ROUNDS
        )


def test_stale_commitment_replans_instead_of_failing(
    context: AlgopyTestContext,
) -> None:
    contract, keeper = _deploy(context)
    with _as(context, contract, keeper.address):
        context.ledger.patch_global_fields(round=1000)
        contract.publish()
        # Keeper outage: jump beyond the ~1000-round seed window.
        context.ledger.patch_global_fields(round=1000 + DELAY_ROUNDS + STALE_MARGIN + 1)
        assert contract.publish() == 0  # replanned, did not raise
        assert contract.reveals.value == 0
        assert contract.target_round.value == (
            1000 + DELAY_ROUNDS + STALE_MARGIN + 1 + DELAY_ROUNDS
        )
