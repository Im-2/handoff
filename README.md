# KeeperHub × Almanak — KeeperHub as Almanak's execution layer

**Almanak decides. KeeperHub executes.** This replaces Almanak's default
Safe + Zodiac execution handoff with a KeeperHub-executed path, on a real
public testnet, with a real chain-verified transaction to show for it.

**Flagship proof**: real Sepolia swap, submitted and confirmed by KeeperHub,
triggered by a real Almanak `PolicyEngine`-approved decision:

- tx: [`0x8611af240e724b3138492d9be35355f81fc5ee438c8640c30bfeaea0d8f187e0`](https://sepolia.etherscan.io/tx/0x8611af240e724b3138492d9be35355f81fc5ee438c8640c30bfeaea0d8f187e0)
- full audit record: [`logs/run-1789501751-93927206.json`](logs/run-1789501751-93927206.json)

## Architecture

```
 ┌──────────────────────────┐        ┌────────────────────────────────┐
 │        ALMANAK           │        │            ADAPTER              │
 │  (decides — unmodified)  │        │   adapter/keeperhub_executor.py │
 │                          │        │                                 │
 │  strategy.decide(market) │        │  class KeeperHubToolExecutor    │
 │   -> SwapIntent          │  swap  │      (ToolExecutor)             │
 │        │                 │_tokens │                                 │
 │        v                 │─tool──>│  overrides ONLY                 │
 │  ToolExecutor.execute()  │  call  │  _dispatch_action()             │
 │        │                 │        │  for tool_name=="swap_tokens"   │
 │        v                 │        │        │                        │
 │  PolicyEngine.check()    │        │        v                        │
 │  (spend limits, chain/   │        │  builds real Sepolia calldata   │
 │   token allowlists, rate │        │  (adapter/sepolia_contracts.py) │
 │   limits, cooldown,      │        │        │                        │
 │   human-approval gate)   │        │        v                        │
 │        │                 │        │  KeeperHub MCP server           │
 │        │  [everything    │        │  (adapter/keeperhub_mcp.py)     │
 │        │   above this    │        │   1. simulate:true (never       │
 │        │   line is real, │        │      broadcast a revert)        │
 │        │   unmodified    │        │   2. real call, idempotency-    │
 │        │   Almanak code] │        │      keyed                      │
 └────────┼─────────────────┘        └──────────────┬──────────────────┘
          │  (normally: gateway CompileIntent/Execute               │
          │   -> Safe + Zodiac Roles Modifier -> mainnet)            │
          │   -- REPLACED, not extended, by the adapter --           v
          │                                              ┌─────────────────────┐
          └───────────────── decision boundary ────────->│   KEEPERHUB          │
                                                           │  (executes)          │
                                                           │  wrap -> approve ->  │
                                                           │  swap on Sepolia     │
                                                           │  Uniswap V3          │
                                                           │        │             │
                                                           │        v             │
                                                           │  Sepolia (public     │
                                                           │  testnet, real tx)   │
                                                           └─────────────────────┘
```

The adapter subclasses Almanak's real `ToolExecutor`
([`almanak/framework/agent_tools/executor.py`](.venv/Lib/site-packages/almanak/framework/agent_tools/executor.py))
and overrides exactly one method, `_dispatch_action`, and only for
`tool_name == "swap_tokens"`. Everything before that call — Pydantic schema
validation, the real `PolicyEngine.check()` (chain/token/protocol
allowlists, spend limits, rate limits, cooldown, circuit breaker,
human-approval gate) — is inherited, unmodified Almanak code. See
[`adapter/keeperhub_executor.py`](adapter/keeperhub_executor.py) for the
whole thing; it's ~200 lines.

## Why the adapter builds its own Sepolia calldata

Almanak's bundled DEX/lending connectors hardcode **mainnet** contract
addresses only. This was confirmed by reading the installed SDK source
directly (`.venv/Lib/site-packages/almanak/core/chains/*.py` and
`connectors/uniswap_v3/`) — there is no Sepolia (or any testnet) deployment
wired into any connector, and `almanak strat new`'s `--chain` choices are
mainnet chain names only. So Almanak's own `compile_intent` path can't
target Sepolia.

Given that, the adapter is designed so **Almanak still makes the real
decision on its own fully-supported chain** (Arbitrum — real market data,
real `PolicyEngine` approval), and the adapter maps that approved decision's
semantics (pair, direction, size) onto a concrete Sepolia transaction, which
it builds itself (`adapter/sepolia_contracts.py`) against contract addresses
verified live on-chain (see that file's docstring — Etherscan contract-name
checks plus a direct `Factory.getPool()` + `liquidity()` read, reproducible
via `scripts/verify_sepolia_pool.py`).

## Repo layout

```
almanak_baseline/     Real almanak strat new -t ta_swap scaffold (untouched
                       except an encoding fix -- see below). The baseline.
adapter/               The integration. Small and isolated:
  sepolia_contracts.py    Sepolia addresses + calldata builders (verified on-chain)
  keeperhub_mcp.py        KeeperHub execution client (MCP server) -- primary path
  keeperhub_cli.py        KeeperHub execution client (kh CLI) -- alternate path
  keeperhub_executor.py   THE ADAPTER: ToolExecutor subclass, one overridden method
  idempotency.py          sha256(reference|chain|recipient|amount|token)
  audit.py                Persists the 3-part audit trail per run
  run_demo.py              End-to-end orchestration script
scripts/
  patch_almanak_windows.py  Windows compatibility patch (see below) -- not
                              part of the integration
  verify_sepolia_pool.py    Re-verify pool addresses/liquidity live
logs/                  Persisted audit records, one JSON per run (tracked in git)
```

## Setup

Prerequisites: Python 3.12 (see "Windows notes" for why not 3.14), a
KeeperHub API key, an Alchemy API key (free tier), and (only if you want the
Anvil baseline proof) [Foundry](https://getfoundry.sh) and Go.

```bash
python -m venv .venv
source .venv/Scripts/activate        # or .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
pip install -e almanak_baseline/     # or: cd almanak_baseline && pip install almanak

cp .env.example .env                 # fill in KH_API_KEY, ALCHEMY_API_KEY
# almanak_baseline/.env already exists (created by `almanak strat new`) --
# edit it to set ALMANAK_PRIVATE_KEY (a throwaway/test key is fine;
# decisioning only ever reads balances/prices, never signs) and
# ALCHEMY_API_KEY (same value as above)
```

If you're on Windows, also run:

```bash
python scripts/patch_almanak_windows.py
```

### Getting a KeeperHub wallet funded on Sepolia

The adapter executes through KeeperHub's org wallet (`kh wallet balance`
shows the address). Fund it with Sepolia ETH from a faucet (e.g.
[Alchemy's Sepolia faucet](https://www.alchemy.com/faucets/ethereum-sepolia)) —
that's the only funding needed; the adapter wraps ETH into WETH itself as
its first step.

## Running it

**1. Almanak baseline (no KeeperHub involved)** — proves the SDK's own
decision pipeline works, using real Arbitrum market data:

```bash
cd almanak_baseline
almanak strat run --dry-run --once
```

**2. The full demo** — Almanak decides, KeeperHub executes on Sepolia:

```bash
# Terminal 1: a standalone gateway for Almanak's Arbitrum decisioning
almanak gateway --standalone --network mainnet --chains arbitrum --insecure

# Terminal 2
python adapter/run_demo.py
```

This will: wrap a small amount of Sepolia ETH into WETH via KeeperHub (skipped
if already funded), build the real `KeeperhubSwapAgentStrategy`, get a real
`decide()` call against live Arbitrum data, run the resulting swap intent
through the real `PolicyEngine`, and — once approved — execute it on Sepolia
via KeeperHub (approve, simulate, then the real swap). It prints every step
and writes a full audit record to `logs/<run_id>.json`.

## Walkthrough of the flagship run

`logs/run-1789501751-93927206.json` is a complete, real run. Summary:

1. **Almanak decided**: `SwapIntent(from_token='WETH', to_token='USDC', amount_usd=1000, max_slippage=0.005)` —
   emitted by the real, unmodified `decide()` in
   [`almanak_baseline/strategy.py`](almanak_baseline/strategy.py), reacting to
   an RSI-overbought condition (see "Why the scenario is forced" below).
2. **Almanak approved**: the real `PolicyEngine.check()` validated it against
   chain/token allowlists, spend limits ($2000 single-trade cap, well above
   the $1000 decided notional), and rate limits — all genuine Almanak policy
   code, not simulated.
3. **KeeperHub executed** (`adapter/keeperhub_executor.py` ->
   `adapter/keeperhub_mcp.py`), three real steps, each in
   `keeperhub_execution_steps` of the audit record:
   - `approve()` on WETH9 for SwapRouter02 — tx
     [`0x0af54270...`](https://sepolia.etherscan.io/tx/0x0af542704e9b76ed95dfc644b0fdd43f5088583b982f866f0f1d7a308fe38748)
   - `simulate: true` call to SwapRouter02.exactInputSingle — confirmed
     `wouldRevert: false` (and a simulated output amount) before anything
     was broadcast
   - the real call — KeeperHub simulated, signed, and broadcast it, and
     returned tx `0x8611af24...` on completion
4. **Result persisted**: tx hash, Sepolia Etherscan link, every KeeperHub
   step's raw response, and Almanak's own `DecisionTracer` trail (every tool
   call, its policy result, timing) — all in the audit JSON.

### Why the scenario is forced

The KeeperHub wallet is fresh (no Arbitrum trading history), so a live
balance read for it on Arbitrum genuinely returns "unavailable" — `decide()`
correctly falls to `Intent.hold(reason="Balance data unavailable")` before it
ever reaches the RSI branch. `run_demo.py` then seeds a synthetic funded
portfolio and an overbought RSI using Almanak's own
`apply_scenario`/`parse_scenario` helpers
([`almanak/framework/cli/_scenario.py`](.venv/Lib/site-packages/almanak/framework/cli/_scenario.py)) —
the identical mechanism `almanak strat test --inject` uses. This is Almanak's
own supported test/demo path, not something invented for this integration;
without it, the only honest alternative script would be "wait until Arbitrum
WETH's RSI happens to cross 70 while this demo is being judged."

### Why SELL, not BUY

`decide()` can emit either `BUY` (USDC → WETH, oversold) or `SELL` (WETH →
USDC, overbought). The adapter's only Sepolia funding source is wrapping the
wallet's own Sepolia ETH into WETH (step 1 of the pipeline) — the wallet
never holds Sepolia USDC. `run_demo.py` forces the overbought/SELL branch so
the decided trade matches what the wallet can actually execute; a forced BUY
would correctly fail for lack of USDC (and did, the first time this was
tried — see the debugging trail below).

## What we found debugging the swap leg

This is left in deliberately — it's real engineering, not polish:

1. First attempt used `kh execute contract-call` (the CLI). `approve()` and
   `execute transfer` worked instantly and reliably. `exactInputSingle`
   (and, in isolation, even `WETH9.deposit()`/`withdraw()`) consistently
   stalled for exactly ~2 minutes and returned "Sponsored transaction was
   submitted but not confirmed in time" — with no tx hash and no mempool
   activity (`eth_getTransactionCount` never incremented). Multiple retries,
   an ABI-encoding fix (tuple args need a named JSON object, not a
   positional array — `params.tokenOut: address is missing` was the first,
   useful error the CLI *did* surface), and a clean CLI reinstall didn't
   change the outcome.
2. Switching the same call to KeeperHub's **MCP server**
   (`adapter/keeperhub_mcp.py`) immediately surfaced the real problem via
   `simulate: true`: first the same encoding issue with a much clearer error,
   then — after fixing that — a clean `Error(STF)` revert (Uniswap's
   "SafeTransferFrom failed", i.e. insufficient allowance). Root cause: an
   earlier isolated debugging call had re-approved the router for `1` wei,
   which *replaces* (not adds to) an ERC20 allowance, silently capping it far
   below the swap amount.
3. Fix: re-approve a generous allowance, simulate again (`wouldRevert:
   false`), then broadcast for real. That produced the flagship tx above.

Net: the CLI's `execute contract-call` path has a real reliability gap for
non-standard-ERC20 methods (worth reporting upstream to KeeperHub); the MCP
path does not share it, and its `simulate: true` mode gave us the honest,
actionable diagnostics the CLI didn't. The adapter now uses MCP as its
primary execution client; `keeperhub_cli.py` is kept for reference and for
diagnostics (`kh wallet balance`, `kh chain list`) where it's reliable.

## Non-negotiables, and how this meets them

- **Real, verifiable public-testnet transactions**: every tx cited above is
  real and independently checkable on `sepolia.etherscan.io`. Nothing is
  fork-only.
- **Almanak decides, KeeperHub only executes**: the adapter's override point
  is strictly *after* `PolicyEngine.check()` returns approved. It never
  short-circuits or bypasses that check, and never decides *what* to trade —
  only *how* the approved trade reaches the chain.
- **Small, isolated adapter**: the whole integration is
  `adapter/` (7 small files). `almanak_baseline/` is an unmodified
  `almanak strat new` scaffold (one encoding fix, see below). The only other
  touched files are inside the installed `almanak` package itself, and only
  for Windows OS-compatibility (never business logic) — see next section.
- **No fabricated success states**: every failure encountered (a wrong swap
  direction, a bad ABI encoding, a stuck CLI call, an insufficient allowance)
  is logged in `logs/*.json` and in this README exactly as it happened.

## Windows compatibility patches (not part of the integration)

Almanak's SDK assumes POSIX (`fcntl`, `loop.add_signal_handler`) in a couple
of places, which don't exist on Windows. `scripts/patch_almanak_windows.py`
patches the **installed package** (not Almanak's source repo, not this
adapter) to use `msvcrt`/`signal.signal` equivalents so `almanak gateway`
and `almanak strat run` can boot locally on Windows at all. Re-run it after
any `pip install`/reinstall of `almanak`. `almanak_baseline/strategy.py` also
needed one cp1252→UTF-8 re-encode (the scaffold generator wrote an em-dash
in the wrong encoding on this machine) — a one-line fix, not a logic change.
Development also used Python 3.12, not 3.14: `nest_asyncio` (an Almanak
dependency) has a real incompatibility with Python 3.14's stricter
`asyncio.timeout()` context requirements that broke live balance/price
fetching entirely on 3.14.

## Known limitations

- **Single pair**: the adapter only knows the Sepolia WETH/USDC pool
  (verified with real liquidity — see `scripts/verify_sepolia_pool.py`).
  Extending to more pairs means adding addresses to
  `adapter/sepolia_contracts.py`.
- **No slippage protection on the Sepolia leg**: `amountOutMinimum` is `0`.
  A production adapter would quote via `QuoterV2` first and set a real
  minimum; this demo prioritized a working, honestly-reported round trip
  over production-grade MEV protection on a testnet with no real value.
  Almanak's own `max_slippage` decision is still recorded in the audit trail.
- **Decision chain ≠ execution chain**: `decide()` runs against Arbitrum
  (Almanak's fully-supported chain); execution happens on Sepolia. This is a
  deliberate, documented consequence of Almanak having no native Sepolia
  deployment (see "Why the adapter builds its own calldata" above) — the
  adapter is the one place that bridges it, and every run's audit record
  makes both chains explicit rather than blurring them.
- **Demo-sized swap amount**: the adapter executes a small fixed WETH amount
  (`DEMO_SWAP_AMOUNT_WETH` in `run_demo.py`), not Almanak's literal decided
  `amount_usd` — Sepolia test tokens have no real value and the test pool's
  depth doesn't match mainnet, so replaying the literal dollar figure would
  be meaningless. The decided figure is still logged for honesty.
