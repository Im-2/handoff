"""KeeperHub execution via its hosted MCP server (https://app.keeperhub.com/mcp).

This is the adapter's primary execution client. It replaced an earlier CLI
(`kh execute contract-call`)-based implementation (still in keeperhub_cli.py)
after live testing on Sepolia showed the CLI's `execute contract-call` path
stalling indefinitely ("Sponsored transaction was submitted but not
confirmed in time") on any non-standard-ERC20 method (WETH9.deposit/withdraw,
SwapRouter02.exactInputSingle), while `execute transfer` and ERC20
`approve`/`transfer` worked reliably. The MCP server's `execute_contract_call`
tool's `simulate: true` mode surfaced the real, actionable cause in each case
(an arg-encoding mismatch, then an "STF" SafeTransferFrom-Failed revert from
an allowance that had been overwritten by an earlier debugging call) where
the CLI only ever reported the generic timeout. See README "What we found
debugging the swap leg" for the full trail with real tx hashes for every
step, including the two CLI stalls that were later found to be spurious.

Follows KeeperHub's own documented best practice: simulate first, only
broadcast once `wouldRevert: false`, then use the same idempotency key
discipline either way.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

KEEPERHUB_MCP_URL = "https://app.keeperhub.com/mcp"


class KeeperHubMcpError(RuntimeError):
    """Raised when KeeperHub's MCP server returns a tool-call error (simulation
    revert, encoding failure, or a real execution failure)."""

    def __init__(self, message: str, *, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass
class McpCallResult:
    success: bool
    raw: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""

    @property
    def tx_hash(self) -> str | None:
        return self.raw.get("transactionHash")

    @property
    def execution_id(self) -> str | None:
        return self.raw.get("executionId")

    @property
    def would_revert(self) -> bool | None:
        return self.raw.get("wouldRevert")

    @property
    def revert_reason(self) -> str | None:
        return self.raw.get("revertReason") or self.raw.get("error")


def _api_key() -> str:
    key = os.environ.get("KH_API_KEY", "")
    if not key:
        raise RuntimeError("KH_API_KEY is not set (see .env.example).")
    return key


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> McpCallResult:
    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {_api_key()}"}, timeout=180.0)
    async with streamable_http_client(KEEPERHUB_MCP_URL, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            texts = [getattr(c, "text", "") for c in result.content]
            raw_text = "\n".join(t for t in texts if t)

            # Tool errors arrive as text starting with "API call failed: <status> - <json>"
            # (see the STF-revert example this module's docstring references).
            parsed: dict[str, Any] = {}
            json_start = raw_text.find("{")
            if json_start != -1:
                try:
                    parsed = json.loads(raw_text[json_start:])
                except json.JSONDecodeError:
                    parsed = {}

            is_error = bool(getattr(result, "isError", False)) or raw_text.startswith("API call failed")
            if is_error:
                raise KeeperHubMcpError(
                    parsed.get("error") or parsed.get("revertReason") or raw_text, raw=raw_text
                )
            return McpCallResult(success=True, raw=parsed, raw_text=raw_text)


async def execute_contract_call(
    *,
    contract_address: str,
    chain_id: str,
    function_name: str,
    function_args: list[Any],
    idempotency_key: str | None = None,
    simulate: bool = False,
) -> McpCallResult:
    """Call KeeperHub's `execute_contract_call` MCP tool.

    `function_args` for a tuple/struct-typed Solidity parameter must be a
    plain dict keyed by the ABI's component names (see module docstring --
    confirmed empirically against SwapRouter02.exactInputSingle).
    """
    args: dict[str, Any] = {
        "contract_address": contract_address,
        "chain_id": chain_id,
        "function_name": function_name,
        "function_args": json.dumps(function_args),
    }
    if simulate:
        args["simulate"] = True
    if idempotency_key:
        args["idempotency_key"] = idempotency_key
    return await _call_tool("execute_contract_call", args)


async def execute_transfer(
    *,
    chain_id: str,
    to: str,
    amount: str,
    token: str = "ETH",
    token_address: str | None = None,
    idempotency_key: str | None = None,
) -> McpCallResult:
    args: dict[str, Any] = {"chain_id": chain_id, "to": to, "amount": amount}
    if token_address:
        args["token_address"] = token_address
    else:
        args["token"] = token
    if idempotency_key:
        args["idempotency_key"] = idempotency_key
    return await _call_tool("execute_transfer", args)
