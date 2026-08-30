# arcron-beacon

A **verifiable-randomness beacon** on Algorand TestNet, ticked by
[Arcron](https://github.com/CorvidLabs/arcron) keepers.

**Unaudited. TestNet only. Not deployed (appId = 0).** Deploy needs a
human's explicit go — see issue #1.

## What it does

An Arcron keeper calls `publish()` on a schedule. The hook alternates two
phases, driven entirely by the contract's own state:

1. **PLAN** — commit to a round `DELAY_ROUNDS` (20, ~1 minute) in the
   future and store it.
2. **REVEAL** — once that round has passed, read its block seed
   (`Block.blk_seed`, the round's VRF seed) and publish it to global state.

The seed of a *future* round cannot be known when the commitment is made,
so each reveal is a public, unpredictable randomness anchor. **Rain-style
draws are the audience**: a draw contract fixes a beacon round at draw
time and resolves against the revealed seed later — no oracle, no trust,
anyone can verify the seed came from the named round.

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
  once via `set_keeper`.

## Layout

```
smart_contracts/beacon/contract.py   the Puya (Algorand Python) source — the whole thing
tests/test_beacon.py                 algorand-python-testing unit tests (mock chain)
docs/                                GitHub Pages split-flap board (NOT DEPLOYED until appId > 0)
docs/deploy.json                     {"appId": 0, ...} — the board's single source of config
.github/workflows/pages.yml          publishes docs/ to Pages
.github/workflows/compile.yml        CI: puyapy compile check + unit tests
```

Source-only on purpose: no compiled artifacts are committed. They are
generated at deploy time by the human doing the deploy.

## Build & test locally

```bash
pip install puyapy==5.10.1 algorand-python-testing py-algorand-sdk
puyapy smart_contracts/beacon/contract.py   # compile check (artifacts not committed)
python -m pytest tests/                      # mock-chain unit tests
```

Mock tests cannot prove keeper integration (inner calls, MBR) — that
belongs to a LocalNet/TestNet e2e at deploy time.

## How a human deploys this later

**TestNet only. Never commit a mnemonic. Never deploy without the human go
(issue #1).**

1. Fund a throwaway TestNet account (dispenser). The address may be
   documented; the mnemonic lives in env/CI secrets, never in git.
2. Compile: `puyapy smart_contracts/beacon/contract.py`.
3. Deploy the app with **zero create args**. Record the app id.
4. Call `set_keeper` with the Arcron TestNet keeper app **769891898**
   (creator-only, one-time).
5. Register an upkeep on keeper 769891898 pointing at `publish()`
   (see issue #2; pick `SKIP_AHEAD` deliberately, not the zero default).
6. Set `"appId"` in `docs/deploy.json` — the board lights up on its own
   (issue #3).

## The board

`docs/` is a split-flap/CRT status board in the spirit of
[corvid-agent/plod](https://github.com/corvid-agent/plod). While
`appId` is 0 it shows **NOT DEPLOYED**. Once `appId > 0` it reads the
app's global state from the public indexer
(`https://testnet-idx.algonode.cloud`) and flaps out the latest revealed
round, its seed, the pending target, and total reveals. Read-only, no
wallet, no keys.

Unaudited. TestNet only. Not deployed.
