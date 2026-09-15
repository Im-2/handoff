"""The adapter: reroutes Almanak's swap_tokens execution from Safe/Zodiac to KeeperHub.

This is the one place the integration "touches" Almanak. `KeeperHubToolExecutor`
subclasses Almanak's real `ToolExecutor` (almanak.framework.agent_tools.executor)
and overrides only `_dispatch_action`. Everything upstream of that call --
Pydantic schema validation, the real `PolicyEngine.check()` (chain/token/
protocol allowlists, spend limits, rate limits, cooldown, circuit breaker,
human-approval gate) -- is unmodified Almanak code, inherited and executed
exactly as it runs in Almanak's own default configuration. Only the terminal
on-chain submission step -- normally `IntentExecutionService` calling the
gateway's CompileIntent/Execute RPCs, which sign and broadcast through the
strategy's Safe + Zodiac Roles Modifier -- is replaced here with a call to
KeeperHub (`adapter/keeperhub_mcp.py`).

Why not touch `compile_intent` itself: Almanak's connectors compile calldata
against mainnet contract addresses only (see adapter/sepolia_contracts.py's
docstring for how this was verified). Overriding at `_dispatch_action`
instead means the real Almanak decision + real Almanak policy gate both run
untouched, and only the address/calldata construction for the Sepolia leg is
this adapter's responsibility.

Execution client: KeeperHub's MCP server (adapter/keeperhub_mcp.py), used
with a simulate-first safety gate -- the swap is never broadcast unless a
prior `simulate: true` call reports `wouldRevert: false`. This mirrors
KeeperHub's own documented best practice and is what caught a real
insufficient-allowance bug during development (see keeperhub_mcp.py's
docstring).
"""

from __future__ import annotations

from typing import Any

from almanak.framework.agent_tools.errors import ExecutionFailedError, ToolValidationError
from almanak.framework.agent_tools.executor import ToolExecutor
from almanak.framework.agent_tools.schemas import ToolResponse, ToolResponseStatus

from adapter import sepolia_contracts as sc
from adapter.idempotency import compute_idempotency_key
from adapter.keeperhub_mcp import KeeperHubMcpError, execute_contract_call

SUPPORTED_SWAP_PAIRS = {frozenset({"WETH", "USDC"})}


class KeeperHubToolExecutor(ToolExecutor):
    """Drop-in replacement for `ToolExecutor` that executes `swap_tokens` via KeeperHub."""

    def __init__(self, *args: Any, run_id: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._run_id = run_id
        self.keeperhub_steps: list[dict[str, Any]] = []

    async def _dispatch_action(self, tool_name: str, args: dict) -> ToolResponse:
        if tool_name != "swap_tokens":
            return await super()._dispatch_action(tool_name, args)
        return await self._execute_swap_via_keeperhub(args)

    async def _execute_swap_via_keeperhub(self, args: dict) -> ToolResponse:
        token_in = args["token_in"].upper()
        token_out = args["token_out"].upper()
        amount = args["amount"]
        dry_run = args.get("dry_run", False)
        wallet = args.get("execution_wallet") or self._wallet_address

        pair = frozenset({token_in, token_out})
        if pair not in SUPPORTED_SWAP_PAIRS:
            raise ToolValidationError(
                f"KeeperHub Sepolia adapter only supports the WETH/USDC pair (got {token_in}->{token_out}). "
                "Extend adapter/sepolia_contracts.py to add more verified Sepolia pairs.",
                tool_name="swap_tokens",
            )
        if not wallet:
            raise ToolValidationError(
                "No execution wallet resolved (pass execution_wallet=<KeeperHub wallet address>).",
                tool_name="swap_tokens",
            )

        amount_in_wei = sc.to_token_units(amount, token_in)
        token_in_addr = sc.TOKENS[token_in]
        token_out_addr = sc.TOKENS[token_out]

        if dry_run:
            return ToolResponse(
                status=ToolResponseStatus.SIMULATED,
                data={
                    "tx_hash": None,
                    "amount_in": amount,
                    "amount_out": "",
                    "effective_price": "",
                    "note": "dry_run=True: no KeeperHub call was made",
                },
            )

        reference = f"{self._run_id}:swap:{token_in}-{token_out}:{amount}"

        # Step 1: approve() -- SwapRouter02 must be allowed to pull token_in
        # from the wallet before exactInputSingle can execute. Approves a
        # generous allowance (not just amount_in_wei) so repeated demo runs
        # don't need to re-approve every time (ERC20 approve REPLACES the
        # allowance, so this is a one-time-per-session cost, not additive).
        approve_amount_wei = max(amount_in_wei * 1000, 10**18)
        approve_key = compute_idempotency_key(
            reference=reference + ":approve",
            chain=sc.CHAIN_ID_STR,
            recipient=sc.SWAP_ROUTER_02,
            amount=str(approve_amount_wei),
            token=token_in_addr,
        )
        approve_method, approve_args = sc.erc20_approve_args(sc.SWAP_ROUTER_02, approve_amount_wei)
        try:
            approve_result = await execute_contract_call(
                contract_address=token_in_addr,
                chain_id=sc.CHAIN_ID_STR,
                function_name=approve_method,
                function_args=approve_args,
                idempotency_key=approve_key,
            )
        except KeeperHubMcpError as e:
            self.keeperhub_steps.append(
                {"step": "approve", "idempotency_key": approve_key, "error": str(e), "raw": e.raw}
            )
            raise ExecutionFailedError(f"KeeperHub approve() failed: {e}", tool_name="swap_tokens") from e
        self.keeperhub_steps.append(
            {"step": "approve", "idempotency_key": approve_key, "result": approve_result.raw}
        )

        # Step 2: simulate the swap first (KeeperHub's documented best
        # practice) -- never broadcast against a call that would revert.
        amount_out_minimum_wei = 0  # demo-scope: see README "Known limitations" re: slippage protection
        method, call_args = sc.exact_input_single_args(
            token_in=token_in_addr,
            token_out=token_out_addr,
            fee=sc.WETH_USDC_POOL_FEE,
            recipient=wallet,
            amount_in_wei=amount_in_wei,
            amount_out_minimum_wei=amount_out_minimum_wei,
        )
        try:
            sim_result = await execute_contract_call(
                contract_address=sc.SWAP_ROUTER_02,
                chain_id=sc.CHAIN_ID_STR,
                function_name=method,
                function_args=call_args,
                simulate=True,
            )
        except KeeperHubMcpError as e:
            self.keeperhub_steps.append({"step": "simulate_swap", "error": str(e), "raw": e.raw})
            usd_amount = await self._estimate_usd_spend(args)
            self._policy_engine.record_trade(usd_amount, success=False, tool_name="swap_tokens")
            raise ExecutionFailedError(
                f"KeeperHub swap simulation reverted, not broadcasting: {e}", tool_name="swap_tokens"
            ) from e
        self.keeperhub_steps.append({"step": "simulate_swap", "result": sim_result.raw})

        # Step 3: broadcast for real, only now that simulation cleared.
        swap_key = compute_idempotency_key(
            reference=reference + ":swap",
            chain=sc.CHAIN_ID_STR,
            recipient=wallet,
            amount=str(amount_in_wei),
            token=token_in_addr,
        )
        try:
            swap_result = await execute_contract_call(
                contract_address=sc.SWAP_ROUTER_02,
                chain_id=sc.CHAIN_ID_STR,
                function_name=method,
                function_args=call_args,
                idempotency_key=swap_key,
            )
        except KeeperHubMcpError as e:
            self.keeperhub_steps.append(
                {"step": "swap", "idempotency_key": swap_key, "error": str(e), "raw": e.raw}
            )
            usd_amount = await self._estimate_usd_spend(args)
            self._policy_engine.record_trade(usd_amount, success=False, tool_name="swap_tokens")
            raise ExecutionFailedError(f"KeeperHub swap execution failed: {e}", tool_name="swap_tokens") from e

        self.keeperhub_steps.append({"step": "swap", "idempotency_key": swap_key, "result": swap_result.raw})

        tx_hash = swap_result.tx_hash
        success = tx_hash is not None

        usd_amount = await self._estimate_usd_spend(args)
        self._policy_engine.record_trade(usd_amount, success=success, tool_name="swap_tokens")

        if not success:
            raise ExecutionFailedError(
                f"KeeperHub swap execution returned no transaction hash (raw={swap_result.raw})",
                tool_name="swap_tokens",
            )

        return ToolResponse(
            status=ToolResponseStatus.SUCCESS,
            data={
                "tx_hash": tx_hash,
                "amount_in": amount,
                "amount_out": "",
                "effective_price": "",
                "keeperhub_execution_id": swap_result.execution_id,
                "explorer_url": f"https://sepolia.etherscan.io/tx/{tx_hash}",
            },
        )
