# arcron-beacon

A **verifiable-randomness beacon** on Algorand TestNet, ticked by
[Arcron](https://github.com/CorvidLabs/arcron) keepers.

**Unaudited. TestNet only. Live: app `770742777`, Arcron upkeep `#112`,
delay 800 rounds, cadence 1,700 rounds (~80 min).**

## What it does

An Arcron keeper calls `publish()` on a schedule. The hook alternates two
phases, driven entirely by the contract's own state:

1. **PLAN** — commit to a round `delay` in the future (default
   `DELAY_ROUNDS` = 20, ~1 minute; creator-configurable once via
   `set_delay`, up to `MAX_DELAY` = 800) and store it.
2. **REVEAL** — once that round has passed, read its block seed
   (`Block.blk_seed`, the round's VRF seed) and publish it to global state.

The seed of a *future* round cannot be known when the commitment is made,
so each reveal is a public, unpredictable randomness anchor. **Rain-style
draws are the audience**: a draw contract fixes a beacon round at draw
time and resolves against the revealed seed later — no oracle, no trust,
anyone can verify the seed came from the named round.

## The cadence invariant

**Keeper cadence must satisfy `cadence ≤ delay + 900` rounds.** The AVM
reads block seeds at most 1000 rounds back, and the reveal fires on the
first keeper call after the target — `cadence − delay` rounds late — so it
must land inside the 900-round `STALE_MARGIN`. In numbers:

| plan delay            | max keeper cadence | ≈ wall time |
| --------------------- | ------------------ | ----------- |
| 20 (default)          | 920 rounds         | ~43 min     |
| 800 (`MAX_DELAY`)     | 1,700 rounds       | ~80 min     |

**Longer cadences (hourly, daily, weekly) are impossible by AVM design** —
the 1000-round seed window is a hard ceiling no contract setting can
raise. A keeper slower than `delay + 900` would hit the stale-commitment
path on every call: each call succeeds and gets paid while nothing ever
reveals. The live deployment runs at the widest possible cadence:
delay 800, interval 1,700 = 800 + 900.

## The traps this contract avoids

Read [docs/integrating.md](https://github.com/CorvidLabs/arcron/blob/main/docs/integrating.md)
in the Arcron repo first. In short:

- **Zero-argument hook.** `publish()` takes no args; Arcron supplies none.
  A keeper decides *when* the call happens, never *what* it says.
- **`Application(keeper).address`, never `itob`.** Arcron's inner call
  comes from the keeper *application account*. Comparing against
  `itob(keeper_app_id)` compares 8 bytes to a 32-byte address and never
  matches.
- **Fail soft.** A hook that rejects gets exponentially backed off by
  keeper bots until the schedule quietly stops. Every no-work path here
  **returns**, nothing asserts after the authorization check — including
  the stale-commitment path: after ~1000 rounds the AVM can no longer read
  a seed, so an abandoned commitment is re-planned, never failed on.
- **Zero uint64 create args.** A create_arg of type uint64 is how a sloppy
  deploy script confuses the keeper app id with a cadence and locks an
  interval at ~68 years. `create()` takes nothing; the keeper is named
  once via `set_keeper`, the plan delay once via `set_delay`.

## Layout

```
smart_contracts/beacon/contract.py   the Puya (Algorand Python) source — the whole thing
tests/test_beacon.py                 algorand-python-testing unit tests (mock chain)
docs/                                GitHub Pages split-flap board
docs/deploy.json                     live config — appId 770742777, upkeepId 112
```

Source-only on purpose: no compiled artifacts are committed. They are
generated at deploy time from this source.

## Deployment record (TestNet, 2026-08-31)

Deployed by corvid-agent under the operator's go-ahead, in the documented
order (deploy → set_keeper → set_delay → register):

1. Create (zero create args): tx
   `JX6AMEAKR66MG5KWSEZLYHWCQSSXNPUHWIOA2TXEZALQPBVBONQA` (round 66835422)
   → **app 770742777**.
2. `set_keeper(769891898)`: tx
   `IOH5LDBSRT22JBAWDU6PHYHGR4TCBAI4XZFDITTRQQIK37AOYSVQ`.
3. `set_delay(800)`: tx
   `UQ4QENEMSF5ACEJTJ7YBYBGOHPPSNG7ZROSDS2UXOXRDEXZUJMPQ` — the maximum
   delay, so the cadence invariant allows the widest upkeep interval.
4. Registered on keeper `769891898`: **upkeep #112**, `publish()`,
   interval **1,700 rounds** = delay 800 + seed margin 900, fee 4,000
   µALGO, SKIP_AHEAD, 0.5 ALGO escrow: group
   `QEQS76FPAUQ2B7X3KOFWDBWLGL63WRC3VGODC5NTF2OATEZUKPZQ` (app-call
   `RUT7RTPTJXINIPNHNOR55LKZJLOVNQK4HNZ6XYTVB4EMD5R4YRMQ`, round 66835537;
   first execution due ~round 66837237).
5. `docs/deploy.json` flipped — the board reads live state.

## Build & test locally

```bash
pip install puyapy==5.10.1 algorand-python-testing py-algorand-sdk
puyapy smart_contracts/beacon/contract.py   # compile check (artifacts not committed)
python -m pytest tests/                      # mock-chain unit tests
```

Verified at authoring time: compiles clean on puyapy 5.10.1; 11/11 unit
tests pass (including a full plan→reveal cycle driven at the documented
maximum cadence, and a boundary test proving the stale-replan path fires
only once the seed window genuinely expires).

## The board

`docs/` is a split-flap/CRT status board in the spirit of
[corvid-agent/plod](https://github.com/corvid-agent/plod). While
`appId` is 0 it shows **NOT DEPLOYED**; with the live id set it reads the
app's global state from the public indexer
(`https://testnet-idx.algonode.cloud`) and flaps out the latest revealed
round, its seed, the pending target, and total reveals. Read-only, no
wallet, no keys.

**Publish pending:** Pages from `docs/` needs enabling in repo settings, and
the token that wrote this repo lacks the `workflow` scope for
`.github/workflows/pages.yml` (copy it from
[corvid-agent/plod](https://github.com/corvid-agent/plod/blob/main/.github/workflows/pages.yml)
when a suitably-scoped credential is available).

Unaudited. TestNet only. Live as app 770742777.
