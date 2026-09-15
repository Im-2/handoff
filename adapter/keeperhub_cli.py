"""Thin wrapper around the `kh` CLI -- KeeperHub execution via its CLI surface.

NOTE: the adapter's hot path (adapter/keeperhub_executor.py) now uses
adapter/keeperhub_mcp.py instead. During development, `kh execute
contract-call --wait` was found to stall indefinitely ("Sponsored
transaction was submitted but not confirmed in time") on any non-standard-
ERC20 method, while the MCP server's `execute_contract_call` tool (same
KeeperHub backend, different client) simulated and executed the identical
calls correctly and surfaced real, actionable errors along the way. See
keeperhub_mcp.py's docstring and the README for the full trail. This module
is kept because `kh execute transfer`/`approve`/`wallet balance`/`chain
list` all worked reliably and remain useful for setup and diagnostics --
and because it demonstrates the CLI integration path the brief also allows.
Every call here is a real KeeperHub API request; nothing in this file
fabricates a result -- a failed `kh` invocation raises KeeperHubCliError
with the CLI's own stderr/stdout attached.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KH_BIN = shutil.which("kh") or str(Path(__file__).resolve().parent.parent / ".tools" / "gobin" / "kh.exe")


class KeeperHubCliError(RuntimeError):
    def __init__(self, message: str, *, returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass
class KeeperHubExecutionResult:
    """Parsed result of one `kh execute ...` call."""

    execution_id: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def tx_hashes(self) -> list[dict[str, Any]]:
        return self.raw.get("transactionHashes") or self.raw.get("receipts") or []

    @property
    def first_tx_hash(self) -> str | None:
        direct = self.raw.get("transactionHash")
        if direct:
            return direct
        hashes = self.tx_hashes
        if not hashes:
            return None
        entry = hashes[0]
        return entry.get("hash") if isinstance(entry, dict) else str(entry)

    @property
    def chain_verified(self) -> bool:
        receipts = self.raw.get("receipts") or []
        return bool(receipts) and all(
            r.get("verified") and r.get("receiptStatus") == "success" for r in receipts
        )


def _await_verified_status(
    execution_id: str, *, attempts: int = 6, delay_s: float = 3.0
) -> KeeperHubExecutionResult | None:
    """Poll `kh execute status --require-verified` after a --wait call returns.

    `--wait` unblocks on terminal status, but per `kh execute status --help`
    the receipt-verification reconciler can lag slightly behind that. This
    gives it a few short retries so the audit trail records the
    chain-verified tx hash (not just an unverified "completed" status).
    Returns None if verification never lands within the budget -- callers
    treat that as "submitted but not yet chain-verified", never as success.
    """
    last: KeeperHubExecutionResult | None = None
    for _ in range(attempts):
        try:
            result = get_execution_status(execution_id, require_verified=True)
            return result
        except KeeperHubCliError as e:
            last = KeeperHubExecutionResult(execution_id=execution_id, status="unverified", raw={"stderr": e.stderr})
            time.sleep(delay_s)
    return last


def _run_kh(args: list[str], *, timeout_s: int = 360) -> dict[str, Any]:
    if not Path(_KH_BIN).exists() and shutil.which("kh") is None:
        raise KeeperHubCliError(
            f"kh CLI not found at {_KH_BIN} or on PATH", returncode=-1, stdout="", stderr=""
        )
    cmd = [_KH_BIN if Path(_KH_BIN).exists() else "kh", *args, "--json"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        raise KeeperHubCliError(
            f"kh {' '.join(args)} failed (exit {proc.returncode})",
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    # --json output may have human-readable setup lines before the final JSON
    # object per `kh execute contract-call --help`'s own documented contract.
    decoder = json.JSONDecoder()
    text = proc.stdout.strip()
    last_obj: dict[str, Any] | None = None
    idx = 0
    while idx < len(text):
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
            last_obj = obj
            idx = end
        except json.JSONDecodeError:
            idx = brace + 1
    if last_obj is None:
        raise KeeperHubCliError(
            f"kh {' '.join(args)} produced no parseable JSON",
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return last_obj


def execute_contract_call(
    *,
    chain_id: str,
    contract: str,
    method: str,
    args: list[Any],
    idempotency_key: str,
    wait: bool = True,
    timeout: str = "5m",
    abi_fragment: dict | None = None,
) -> KeeperHubExecutionResult:
    """Call `kh execute contract-call`. Real HTTP call to KeeperHub -- no mocking."""
    cli_args = [
        "execute",
        "contract-call",
        "--chain",
        chain_id,
        "--contract",
        contract,
        "--method",
        method,
        "--args",
        json.dumps(args),
        "--idempotency-key",
        idempotency_key,
    ]
    tmp_abi_path: str | None = None
    try:
        if abi_fragment is not None:
            fd, tmp_abi_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump([abi_fragment], f)
            cli_args += ["--abi-file", tmp_abi_path]
        if wait:
            cli_args += ["--wait", "--timeout", timeout]
        result = _run_kh(cli_args)
    finally:
        if tmp_abi_path:
            try:
                os.unlink(tmp_abi_path)
            except OSError:
                pass
    execution_id = result.get("executionId") or result.get("id") or ""
    if wait and execution_id:
        verified = _await_verified_status(execution_id)
        if verified is not None:
            return verified
    return KeeperHubExecutionResult(
        execution_id=execution_id,
        status=result.get("status", "unknown"),
        raw=result,
    )


def execute_transfer(
    *,
    chain_id: str,
    to: str,
    amount: str,
    idempotency_key: str,
    token: str = "ETH",
    token_address: str | None = None,
    wait: bool = True,
    timeout: str = "5m",
) -> KeeperHubExecutionResult:
    """Call `kh execute transfer`. Real HTTP call to KeeperHub -- no mocking."""
    cli_args = [
        "execute",
        "transfer",
        "--chain",
        chain_id,
        "--to",
        to,
        "--amount",
        amount,
        "--idempotency-key",
        idempotency_key,
    ]
    if token_address:
        cli_args += ["--token-address", token_address]
    else:
        cli_args += ["--token", token]
    if wait:
        cli_args += ["--wait", "--timeout", timeout]
    result = _run_kh(cli_args)
    execution_id = result.get("executionId") or result.get("id") or ""
    if wait and execution_id:
        verified = _await_verified_status(execution_id)
        if verified is not None:
            return verified
    return KeeperHubExecutionResult(
        execution_id=execution_id,
        status=result.get("status", "unknown"),
        raw=result,
    )


def get_execution_status(execution_id: str, *, require_verified: bool = False) -> KeeperHubExecutionResult:
    cli_args = ["execute", "status", execution_id]
    if require_verified:
        cli_args.append("--require-verified")
    result = _run_kh(cli_args)
    return KeeperHubExecutionResult(
        execution_id=result.get("executionId") or execution_id,
        status=result.get("status", "unknown"),
        raw=result,
    )


def wallet_balance() -> dict[str, Any]:
    return _run_kh(["wallet", "balance"])


def chain_list() -> dict[str, Any]:
    return _run_kh(["chain", "list"])


def auth_status() -> dict[str, Any]:
    return _run_kh(["auth", "status"])
