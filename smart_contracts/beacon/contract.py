# pyright: reportMissingModuleSource=false
"""BEACON — a verifiable-randomness anchor on Algorand TestNet.

An Arcron keeper calls `publish()` on a cadence. The hook alternates two
phases, both driven entirely by this contract's own state (Arcron supplies
no arguments, ever):

  1. PLAN   — commit to a round DELAY_ROUNDS in the future.
  2. REVEAL — once that round has passed, read its block seed and publish it.

The seed of a future round is not knowable when the commitment is made, so
each reveal is a public, unpredictable randomness anchor. Rain-style draws
can fix a beacon round at draw time and resolve against the revealed seed.

TRAPS this contract avoids (see docs/integrating.md in CorvidLabs/arcron):

  * Zero-argument hook. A keeper decides *when* publish runs, never *what*
    it says. Everything it needs is on-chain before it runs.
  * Authorization is Application(keeper).address — the sender of Arcron's
    inner call. Never compare against itob(keeper_app_id); that is 8 bytes,
    not an address.
  * FAIL SOFT. A hook that rejects gets backed off by keeper bots (1, 2,
    4... intervals) until the schedule quietly stops. Every no-work path
    here RETURNS, nothing asserts after the authorization check.
  * Zero uint64 create args. A create_arg of type uint64 is how a sloppy
    deploy script confuses the keeper app id with a cadence and locks an
    interval at ~68 years. There is nothing to pass at create; the keeper
    is named once via `set_keeper`.

TestNet only. Unaudited. Not deployed (appId = 0 until a human deploys).
"""

from typing import Final

from algopy import (
    ARC4Contract,
    Application,
    Bytes,
    Global,
    GlobalState,
    Txn,
    UInt64,
)
from algopy.arc4 import abimethod
from algopy.op import Block

# Rounds are ~2.8 s on TestNet, so 20 rounds commits to a seed ~1 minute
# out: far enough to be unknowable at plan time, near enough that a weekly
# or daily keeper cadence always finds it readable.
DELAY_ROUNDS: Final = 20

# The AVM `block` opcode can only read seeds up to 1000 rounds back. If a
# keeper outage leaves a commitment older than this margin, we abandon it
# and re-plan instead of letting the reveal call fail (fail-soft).
STALE_MARGIN: Final = 900


class Beacon(ARC4Contract):
    """Future-block-seed randomness beacon, ticked by Arcron keepers."""

    def __init__(self) -> None:
        # App id of the Arcron keeper allowed to drive `publish`. Zero until
        # `set_keeper`. Not an interval. Not a create arg.
        self.keeper_app = GlobalState(UInt64(0))
        # The round we are committed to reveal. Zero means "plan next call".
        self.target_round = GlobalState(UInt64(0))
        # Last round whose seed was published, and the seed itself.
        self.revealed_round = GlobalState(UInt64(0))
        self.revealed_seed = GlobalState(Bytes())
        # How many seeds have been published, ever.
        self.reveals = GlobalState(UInt64(0))

    @abimethod(create="require")
    def create(self) -> None:
        """No-op create. Zero arguments on purpose.

        The 68-year cadence trap: never take a uint64 create arg that a
        deploy script might map to the keeper app id. Nothing to pass here.
        """
        self.keeper_app.value = UInt64(0)
        self.target_round.value = UInt64(0)
        self.revealed_round.value = UInt64(0)
        self.revealed_seed.value = Bytes()
        self.reveals.value = UInt64(0)

    @abimethod()
    def set_keeper(self, keeper: Application) -> None:
        """Name the Arcron keeper whose app account may call `publish`.

        Creator-only, one-time. Pass the keeper *application*, not a raw
        uint64. `publish` authorizes Application(keeper).address — the
        inner-call sender when Arcron `execute()` inner-calls this app —
        never itob(keeper.id).
        """
        assert Txn.sender == Global.creator_address, "Only the creator can set the keeper"
        assert self.keeper_app.value == 0, "Keeper already set"
        assert keeper.id != 0, "Keeper app required"
        self.keeper_app.value = keeper.id

    @abimethod()
    def publish(self) -> UInt64:
        """Arcron hook. Zero arguments; the selector is the only app arg.

        PLAN:   if no target is committed, commit to Global.round +
                DELAY_ROUNDS and return 0.
        REVEAL: if the target round has passed, publish its block seed,
                return the revealed round, and clear the commitment so the
                next call plans again.

        Every no-work path returns rather than asserting: a hook that fails
        gets exponentially backed off by keeper bots and stops being
        serviced. Nothing here may reject after the authorization check.
        """
        keeper = self.keeper_app.value
        assert keeper != 0, "Keeper not set"
        # Inner-call sender is the keeper *app account*, not itob(keeper.id).
        assert (
            Txn.sender == Application(keeper).address
        ), "Only the keeper app may publish"

        now = Global.round

        # PLAN — nothing committed, so commit to a future round.
        if self.target_round.value == 0:
            self.target_round.value = now + UInt64(DELAY_ROUNDS)
            return UInt64(0)

        target = self.target_round.value

        # Too early — the round has not happened. Return, do not assert.
        if now <= target:
            return UInt64(0)

        # Already revealed this round (defensive; target clears on reveal).
        if self.revealed_round.value == target:
            return UInt64(0)

        # Stale commitment: after ~1000 rounds the AVM can no longer read the
        # seed. Abandon it and re-plan rather than let blk_seed fail the call.
        if now - target > STALE_MARGIN:
            self.target_round.value = now + UInt64(DELAY_ROUNDS)
            return UInt64(0)

        # REVEAL — read the seed of the round we committed to earlier.
        self.revealed_seed.value = Block.blk_seed(target)
        self.revealed_round.value = target
        self.reveals.value += 1
        self.target_round.value = UInt64(0)
        return target

    @abimethod(readonly=True)
    def latest(self) -> UInt64:
        """The last round whose seed is published (0 = none yet)."""
        return self.revealed_round.value
