"""Persists the three-part audit trail the brief asks for, per run:

  1. Almanak's own decision trace (DecisionTracer entries: tool calls, real
     PolicyEngine results, timing) -- proof of *why* it decided to act.
  2. KeeperHub's execution steps (from KeeperHubToolExecutor.keeperhub_steps)
     -- trigger -> simulation/approve -> submitted tx -> confirmed outcome.
  3. The resulting onchain transaction hash(es) + explorer link(s).

Nothing here fabricates a result: a failed run is written with its real
error attached, never silently dropped or rewritten as a success.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)


def save_run_record(
    *,
    run_id: str,
    strategy_name: str,
    chain: str,
    almanak_trace_entries: list[Any],
    keeperhub_steps: list[dict[str, Any]],
    final_status: str,
    tx_hashes: list[str],
    explorer_urls: list[str],
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "strategy_name": strategy_name,
        "chain": chain,
        "saved_at": datetime.now(UTC).isoformat(),
        "final_status": final_status,
        "tx_hashes": tx_hashes,
        "explorer_urls": explorer_urls,
        "error": error,
        "almanak_decision_trace": [
            e.__dict__ if hasattr(e, "__dict__") else e for e in almanak_trace_entries
        ],
        "keeperhub_execution_steps": keeperhub_steps,
        "extra": extra or {},
    }
    path = LOGS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(record, indent=2, default=_json_default), encoding="utf-8")
    return path
