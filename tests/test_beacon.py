"""Unit tests for the Beacon contract using algorand-python-testing.

These mock-chain tests cover authorization, the plan/reveal state machine,
and fail-soft behaviour. Per docs/integrating.md in CorvidLabs/arcron,
mocked tests cannot prove keeper integration (inner-call execution, MBR);
that proof belongs to a LocalNet/TestNet e2e at deploy time.

Note: the mock chain's Block.blk_seed returns itob(seed_int); on a real
chain it is a 32-byte VRF seed. The contract stores whatever the AVM
returns either way.
"""

import pytest
from algopy.op import itob
from algopy_testing import AlgopyTestContext, algopy_testing_context

from smart_contracts.beacon.contract import DELAY_ROUNDS, STALE_MARGIN, Beacon


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
