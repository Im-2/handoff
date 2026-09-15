"""Read-only backend API for the Handoff project.

Serves the persisted run records in logs/*.json (Almanak's decision trace +
KeeperHub's execution steps + real Sepolia tx hashes) over HTTP. Nothing
here executes a transaction or holds a secret -- it only reads files that
are already committed to the repo. A "trigger a new run" endpoint is a
deliberate non-goal for this first pass (see README "Live API") until it
can be built with proper auth/rate-limiting in front of real spend.

Run locally:
    uvicorn server.main:app --reload --port 8000
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"

app = FastAPI(
    title="Handoff API",
    description="Almanak decides, KeeperHub executes -- read-only run history.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _load_run(run_id: str) -> dict[str, Any]:
    path = LOGS_DIR / f"{run_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'")
    return json.loads(path.read_text(encoding="utf-8"))


def _list_runs() -> list[dict[str, Any]]:
    runs = []
    for path in sorted(LOGS_DIR.glob("run-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        runs.append(
            {
                "run_id": data.get("run_id", path.stem),
                "strategy_name": data.get("strategy_name"),
                "chain": data.get("chain"),
                "saved_at": data.get("saved_at"),
                "final_status": data.get("final_status"),
                "tx_hashes": data.get("tx_hashes", []),
                "explorer_urls": data.get("explorer_urls", []),
                "error": data.get("error"),
            }
        )
    runs.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
    return runs


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    return _list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return _load_run(run_id)


@app.get("/api/flagship")
def flagship() -> dict[str, Any]:
    """The one successful, fully-clean run cited in the README."""
    runs = [r for r in _list_runs() if r["final_status"] == "success" and r["tx_hashes"]]
    if not runs:
        raise HTTPException(status_code=404, detail="No successful run recorded yet")
    return _load_run(runs[0]["run_id"])


def _render_landing_page() -> str:
    runs = _list_runs()
    flagship_run = next((r for r in runs if r["final_status"] == "success" and r["tx_hashes"]), None)

    rows = "\n".join(
        f"""
        <tr>
          <td><a href="/api/runs/{r['run_id']}">{r['run_id']}</a></td>
          <td><span class="status status-{r['final_status']}">{r['final_status']}</span></td>
          <td>{r['saved_at'] or ''}</td>
          <td>{
            ' '.join(
                f'<a href="{url}" target="_blank" rel="noopener">{tx[:10]}...</a>'
                for tx, url in zip(r['tx_hashes'], r['explorer_urls'])
            ) if r['tx_hashes'] else (r['error'] or '')[:80]
          }</td>
        </tr>"""
        for r in runs
    )

    flagship_block = ""
    if flagship_run:
        tx = flagship_run["tx_hashes"][0]
        url = flagship_run["explorer_urls"][0]
        flagship_block = f"""
        <div class="flagship">
          <h2>Flagship proof</h2>
          <p>Almanak decided a swap, its real PolicyEngine approved it, KeeperHub executed it on Sepolia:</p>
          <a class="tx-link" href="{url}" target="_blank" rel="noopener">{tx}</a>
          <p><a href="/api/runs/{flagship_run['run_id']}">full audit record (JSON)</a></p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handoff — Almanak x KeeperHub</title>
<style>
  :root {{ --bg: #0b0f14; --panel: #121821; --border: #232c38; --text: #e6edf3;
           --muted: #8b98a5; --accent: #58a6ff; --ok: #3fb950; --err: #f85149; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
          font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  main {{ max-width: 900px; margin: 0 auto; padding: 40px 20px 80px; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); margin-top: 0; }}
  .flagship {{ background: var(--panel); border: 1px solid var(--border);
               border-radius: 10px; padding: 20px 24px; margin: 28px 0; }}
  .flagship h2 {{ margin-top: 0; font-size: 1.1rem; }}
  .tx-link {{ display: inline-block; font-family: ui-monospace, monospace; font-size: 0.85rem;
              color: var(--accent); word-break: break-all; text-decoration: none; }}
  .tx-link:hover {{ text-decoration: underline; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 0.9rem; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 500; }}
  a {{ color: var(--accent); }}
  .status {{ padding: 2px 8px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; }}
  .status-success {{ background: rgba(63,185,80,0.15); color: var(--ok); }}
  .status-error {{ background: rgba(248,81,73,0.15); color: var(--err); }}
  code {{ background: var(--panel); padding: 1px 6px; border-radius: 4px; font-size: 0.85em; }}
  footer {{ margin-top: 40px; color: var(--muted); font-size: 0.85rem; }}
</style>
</head>
<body>
<main>
  <h1>Handoff</h1>
  <p class="subtitle">Almanak decides. KeeperHub executes. Read-only view of every run's real, on-chain proof.</p>
  {flagship_block}
  <h2>All runs</h2>
  <table>
    <thead><tr><th>Run</th><th>Status</th><th>When</th><th>Result</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <footer>
    API: <a href="/api/runs">/api/runs</a> &middot; <a href="/api/flagship">/api/flagship</a> &middot;
    <a href="/docs">/docs</a> (OpenAPI) &middot;
    <a href="https://github.com/Im-2/handoff" target="_blank" rel="noopener">source</a>
  </footer>
</main>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def landing_page() -> str:
    return _render_landing_page()
