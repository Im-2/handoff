"""Deterministic idempotency keys for KeeperHub write calls.

Per the brief: sha256 of reference|chain|recipient|amount|token. Same inputs
always produce the same key, so a retried adapter call (e.g. after a network
blip) is deduplicated by KeeperHub rather than double-submitted.
"""

from __future__ import annotations

import hashlib


def compute_idempotency_key(*, reference: str, chain: str, recipient: str, amount: str, token: str) -> str:
    payload = f"{reference}|{chain}|{recipient}|{amount}|{token}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
