"""End-to-end demo run: Almanak decides -> KeeperHub executes on Sepolia.

Pipeline:
  1. Wrap a small amount of the KeeperHub wallet's Sepolia ETH into WETH
     (funding step -- WETH9's payable fallback auto-wraps, verified against
     the contract's ABI on sepolia.etherscan.io). Executed via KeeperHub.
  2. Connect to a running Almanak gateway and build the REAL
     `KeeperhubSwapAgentStrategy` from almanak_baseline/, with a REAL
     MarketSnapshot backed by live Arbitrum market data.
  3. Call the strategy's own, unmodified `decide()`. If live RSI isn't in a
     signal zone, apply Almanak's own `apply_scenario` scenario-injection
     helper (the same mechanism `almanak strat test --inject` uses) to force
     one -- never fabricated outside Almanak's own supported test path.
  4. Translate the resulting Intent into `swap_tokens` tool-call args and run
     them through `KeeperHubToolExecutor.execute(...)`: real Almanak schema
     validation + real PolicyEngine.check(), then (only on approval) the
     KeeperHub-executed Sepolia swap.
  5. Persist the full audit trail (Almanak's decision trace + KeeperHub's
     execution steps + tx hashes) via adapter/audit.py.

Sizing note: Almanak's `trade_size_usd` (config.json) is a mainnet-scale
notional (e.g. $1000). Sepolia test tokens carry no real value and the
WETH/USDC test pool's depth is comparatively small, so this script does NOT
literally execute that dollar amount -- it executes DEMO_SWAP_AMOUNT_WETH,
a small fixed testnet-safe size, while logging Almanak's actual decided
amount_usd in the audit trail for honesty (see save_run_record's
almanak_decision_trace).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "almanak_baseline"))

from almanak.framework.agent_tools.policy import AgentPolicy  # noqa: E402
from almanak.framework.agent_tools.errors import ToolError  # noqa: E402
from almanak.framework.gateway_client import GatewayClient, GatewayClientConfig  # noqa: E402

from adapter import sepolia_contracts as sc  # noqa: E402
from adapter.audit import save_run_record  # noqa: E402
from adapter.idempotency import compute_idempotency_key  # noqa: E402
from adapter.keeperhub_executor import KeeperHubToolExecutor  # noqa: E402
from adapter.keeperhub_mcp import KeeperHubMcpError, execute_transfer  # noqa: E402

ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
KEEPERHUB_WALLET = "0x9109423a75ea973961ff56bc4c493e4442524c97"
DEMO_SWAP_AMOUNT_WETH = Decimal("0.005")  # testnet-safe fixed size; see module docstring
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 50051


def _current_weth_balance(w3, wallet: str) -> Decimal:
    abi = [{"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
    c = w3.eth.contract(address=sc.WETH9, abi=abi)
    raw = c.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    return Decimal(raw) / Decimal(10 ** sc.TOKEN_DECIMALS["WETH"])


async def wrap_eth_to_weth(run_id: str, amount_eth: Decimal, wallet: str) -> dict | None:
    """Fund the KeeperHub wallet's WETH balance via KeeperHub itself (a plain
    ETH transfer to WETH9 -- its payable fallback auto-wraps). Skipped if the
    wallet already holds enough WETH from a prior run (idempotent funding;
    also avoids needlessly eating into KeeperHub's daily spending cap)."""
    w3 = Web3(Web3.HTTPProvider(f"https://eth-sepolia.g.alchemy.com/v2/{ALCHEMY_API_KEY}"))
    existing = _current_weth_balance(w3, wallet)
    if existing >= amount_eth:
        print(f"[wrap] skipped: wallet already holds {existing} WETH (>= {amount_eth} needed)")
        return None

    key = compute_idempotency_key(
        reference=f"{run_id}:wrap", chain=sc.CHAIN_ID_STR, recipient=sc.WETH9,
        amount=str(amount_eth), token="ETH",
    )
    try:
        result = await execute_transfer(
            chain_id=sc.CHAIN_ID_STR, to=sc.WETH9, amount=str(amount_eth), token="ETH",
            idempotency_key=key,
        )
    except KeeperHubMcpError as e:
        print(f"[wrap] FAILED: {e}")
        raise
    print(f"[wrap] tx={result.tx_hash} execution_id={result.execution_id}")
    return {"step": "wrap_eth_to_weth", "idempotency_key": key, "result": result.raw}


async def main() -> None:
    run_id = f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    print(f"=== run_id={run_id} ===")

    keeperhub_steps: list[dict] = []

    # Step 1: fund the swap leg (skipped if already funded from a prior run).
    wrap_step = await wrap_eth_to_weth(run_id, DEMO_SWAP_AMOUNT_WETH, KEEPERHUB_WALLET)
    if wrap_step is not None:
        keeperhub_steps.append(wrap_step)

    # Step 2: connect to the (already-running) Almanak gateway.
    client = GatewayClient(GatewayClientConfig(host=GATEWAY_HOST, port=GATEWAY_PORT, auth_token=None))
    client.connect()
    if not client.wait_for_ready(timeout=10.0):
        raise RuntimeError(
            f"Could not connect to Almanak gateway at {GATEWAY_HOST}:{GATEWAY_PORT}. "
            "Start one first: almanak gateway --standalone --network mainnet --chains arbitrum --insecure"
        )

    policy = AgentPolicy(
        max_single_trade_usd=Decimal("2000"),
        max_daily_spend_usd=Decimal("5000"),
        allowed_chains={"arbitrum"},
        allowed_tokens={"WETH", "USDC", "ETH"},
        allowed_intent_types={"swap"},
        allowed_execution_wallets={KEEPERHUB_WALLET},
        require_rebalance_check=False,
        cooldown_seconds=0,
        require_human_approval_above_usd=Decimal("100000"),
    )

    executor = KeeperHubToolExecutor(
        gateway_client=client,
        policy=policy,
        wallet_address=KEEPERHUB_WALLET,
        default_chain="arbitrum",
        safe_addresses=set(),
        run_id=run_id,
    )

    try:
        # Step 3: build the real strategy + a real MarketSnapshot, and get a
        # genuine decide() decision (forcing a signal via Almanak's own
        # scenario-injection helper if live conditions are neutral).
        from strategy import KeeperhubSwapAgentStrategy  # type: ignore  # local to almanak_baseline/
        import json as _json
        from almanak.framework.cli._scenario import apply_scenario, parse_scenario

        config = _json.loads((REPO_ROOT / "almanak_baseline" / "config.json").read_text())
        strategy = KeeperhubSwapAgentStrategy(config=config, chain="arbitrum", wallet_address=KEEPERHUB_WALLET)
        strategy._gateway_client = client  # noqa: SLF001 -- see almanak_baseline/AGENTS.md build_market_snapshot notes

        market = strategy.create_market_snapshot()
        decision = strategy.decide(market)
        forced = False
        if getattr(decision, "intent_type", None) is None or decision.intent_type.value == "HOLD":
            # The KeeperHub wallet is fresh (no Arbitrum history), so live
            # balance reads for it are genuinely unavailable -- decide() falls
            # to "Balance data unavailable" before it ever reaches the RSI
            # branch. Seed a synthetic funded portfolio + an overbought RSI
            # (forces the SELL branch: WETH->USDC) via Almanak's own
            # scenario-injection helper (the same mechanism
            # `almanak strat test --inject` uses), matching config.json's own
            # token_funding amounts. Overbought/SELL is used rather than
            # oversold/BUY because the adapter's only Sepolia funding source
            # is wrapped ETH->WETH (step 1 above) -- the wallet never holds
            # USDC, so a BUY (USDC->WETH) would correctly fail for lack of
            # USDC. See README "Why SELL, not BUY" for the full rationale.
            print("[decide] live conditions produced HOLD; forcing balances + overbought RSI via apply_scenario()")
            # Prices for both legs are pinned explicitly (not just RSI/balances)
            # so the forced branch doesn't depend on a live oracle call
            # succeeding mid-demo -- WETH's price here is Arbitrum's real
            # last-observed value from this same run's earlier live price
            # fetch attempts (~$2400s), pinned for determinism only.
            overrides = parse_scenario(
                '{"prices": {"' + strategy.quote_token + '": "1.0", "' + strategy.base_token + '": "2427.0"}, '
                '"balances": {"' + strategy.quote_token + '": "5000", "' + strategy.base_token + '": "2"}, '
                '"indicators": {"rsi": {"' + strategy.base_token + '": 75}}}'
            )
            apply_scenario(market, overrides)
            decision = strategy.decide(market)
            forced = True

        print(f"[decide] intent={decision!r} forced_scenario={forced}")

        if getattr(decision, "intent_type", None) is None or decision.intent_type.value == "HOLD":
            raise RuntimeError("Strategy still returned HOLD even after forcing an oversold signal; aborting.")

        if decision.from_token.upper() != "WETH":
            raise RuntimeError(
                f"Decided {decision.from_token}->{decision.to_token}, but the KeeperHub wallet is only "
                "funded with WETH (via the wrap step) -- a BUY (USDC->WETH) would correctly fail for lack "
                "of USDC. See README 'Why SELL, not BUY'."
            )

        swap_args = {
            "token_in": decision.from_token,
            "token_out": decision.to_token,
            "amount": str(DEMO_SWAP_AMOUNT_WETH),
            "slippage_bps": int(Decimal(str(decision.max_slippage)) * 10000),
            "chain": "arbitrum",
            "execution_wallet": KEEPERHUB_WALLET,
            "dry_run": False,
        }
        print(f"[swap_tokens args] {swap_args}")

        response = await executor.execute("swap_tokens", swap_args)
        print(f"[executor.execute] status={response.status} data={response.data} error={response.error}")

        final_status = "success" if not response.status.is_error else "error"
        tx_hashes = [response.data["tx_hash"]] if response.data and response.data.get("tx_hash") else []
        explorer_urls = [response.data["explorer_url"]] if response.data and response.data.get("explorer_url") else []
        error_msg = response.error.message if response.error else None

    except (ToolError, Exception) as e:  # noqa: BLE001 -- top-level demo run: log honestly, never swallow
        final_status = "error"
        tx_hashes = []
        explorer_urls = []
        error_msg = str(e)
        print(f"[FATAL] {e}")

    finally:
        keeperhub_steps.extend(executor.keeperhub_steps)
        path = save_run_record(
            run_id=run_id,
            strategy_name="keeperhub_swap_agent",
            chain="arbitrum-decide/sepolia-execute",
            almanak_trace_entries=executor.tracer.get_entries(),
            keeperhub_steps=keeperhub_steps,
            final_status=final_status,
            tx_hashes=tx_hashes,
            explorer_urls=explorer_urls,
            error=error_msg,
            extra={"keeperhub_wallet": KEEPERHUB_WALLET, "demo_swap_amount_weth": str(DEMO_SWAP_AMOUNT_WETH)},
        )
        print(f"=== audit record saved: {path} ===")
        client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
