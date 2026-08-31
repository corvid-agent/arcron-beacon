# pyright: reportMissingModuleSource=false
"""BEACON — a verifiable-randomness anchor on Algorand TestNet.

An Arcron keeper calls `publish()` on a cadence. The hook alternates two
phases, both driven entirely by this contract's own state (Arcron supplies
no arguments, ever):

  1. PLAN   — commit to a round `delay` in the future.
  2. REVEAL — once that round has passed, read its block seed and publish it.

The seed of a future round is not knowable when the commitment is made, so
each reveal is a public, unpredictable randomness anchor. Rain-style draws
can fix a beacon round at draw time and resolve against the revealed seed.

CADENCE INVARIANT (the one rule a deployer must respect):

    keeper cadence  <=  delay + STALE_MARGIN

The AVM `block` opcode reads seeds at most 1000 rounds back, so a commit
goes unreadable STALE_MARGIN (900) rounds after its target. The reveal
fires on the first keeper call after the target, i.e. `cadence - delay`
rounds late; it must land inside the 900-round margin. `delay` defaults to
DELAY_ROUNDS (20) and may be set once by the creator via `set_delay`, in
[1, MAX_DELAY]. With the default, any cadence up to 920 rounds (~43 min at
2.8 s/round) works; with delay = 800, up to 1700 rounds (~80 min). Longer
cadences are impossible by AVM design — the 1000-round seed window is a
hard ceiling, and no contract setting changes it. A keeper slower than
`delay + 900` would re-plan forever: every call succeeds, every call gets
paid, nothing ever reveals. Pick the upkeep interval accordingly.

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
    is named once via `set_keeper`, and the plan delay once via
    `set_delay` — both admin methods, both after deploy.

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

# Default plan delay, used until `set_delay` overrides it. Rounds are
# ~2.8 s on TestNet, so 20 rounds commits to a seed ~1 minute out: far
# enough to be unknowable at plan time. With this default the keeper
# cadence must be <= 920 rounds (~43 min); see the module docstring.
DELAY_ROUNDS: Final = 20

# Largest delay `set_delay` accepts. Capped so the plan target always sits
# comfortably inside the 1000-round blk_seed window no matter when the
# reveal call lands within it, and so the cadence ceiling
# (delay + STALE_MARGIN) stays honest: at most 800 + 900 = 1700 rounds.
MAX_DELAY: Final = 800

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
        # Plan delay override. Zero means "use DELAY_ROUNDS"; set once by
        # the creator via `set_delay`. Never a create arg.
        self.delay_rounds = GlobalState(UInt64(0))
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
        self.delay_rounds.value = UInt64(0)
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
    def set_delay(self, delay: UInt64) -> None:
        """Set the plan delay: how far ahead of a PLAN call the commit
        target is placed. Creator-only, one-time; defaults to DELAY_ROUNDS.

        Bounded to [1, MAX_DELAY]. The bound is what keeps the cadence
        invariant `cadence <= delay + STALE_MARGIN` satisfiable and honest:
        even at MAX_DELAY the ceiling is 1700 rounds (~80 min), still far
        inside the 1000-round blk_seed window the reveal depends on. Set
        this before registering the upkeep, and pick the upkeep interval
        <= delay + 900 rounds.
        """
        assert Txn.sender == Global.creator_address, "Only the creator can set the delay"
        assert self.delay_rounds.value == 0, "Delay already set"
        assert delay >= 1, "Delay must be at least 1 round"
        assert delay <= MAX_DELAY, "Delay exceeds the blk_seed window"
        self.delay_rounds.value = delay

    def _delay(self) -> UInt64:
        """Effective plan delay: the override once set, else DELAY_ROUNDS."""
        override = self.delay_rounds.value
        if override == 0:
            return UInt64(DELAY_ROUNDS)
        return override

    @abimethod()
    def publish(self) -> UInt64:
        """Arcron hook. Zero arguments; the selector is the only app arg.

        PLAN:   if no target is committed, commit to Global.round + delay
                and return 0.
        REVEAL: if the target round has passed, publish its block seed,
                return the revealed round, and clear the commitment so the
                next call plans again.

        Every no-work path returns rather than asserting: a hook that fails
        gets exponentially backed off by keeper bots and stops being
        serviced. Nothing here may reject after the authorization check.

        The reveal only fires while the target is still inside the AVM's
        1000-round blk_seed window (now - target <= STALE_MARGIN). The
        keeper cadence must therefore satisfy `cadence <= delay + 900` —
        see the module docstring. A slower cadence is a configuration
        error, not something this hook can signal from inside a fail-soft
        design; it is ruled out at registration time, not here.
        """
        keeper = self.keeper_app.value
        assert keeper != 0, "Keeper not set"
        # Inner-call sender is the keeper *app account*, not itob(keeper.id).
        assert (
            Txn.sender == Application(keeper).address
        ), "Only the keeper app may publish"

        now = Global.round
        delay = self._delay()

        # PLAN — nothing committed, so commit to a future round.
        if self.target_round.value == 0:
            self.target_round.value = now + delay
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
            self.target_round.value = now + delay
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
