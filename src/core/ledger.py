"""Ledger state persistence, CRUD operations, and summarize helpers."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.core.config import CONTROL_PATH, KEEP_DAYS, LEDGER_PATH, TZ, load_json, save_json
from src.parser.amount_parser import is_reference_match, normalize_search

LEDGER_LOCK = threading.RLock()


def load_control() -> dict:
    control = load_json(CONTROL_PATH, {})
    control.setdefault("maintenance", False)
    control.setdefault("closed_days", [])
    return control


def save_control(control: dict) -> None:
    save_json(CONTROL_PATH, control)


def control_state(cfg: dict) -> dict:
    return cfg.setdefault("_control", {"maintenance": False, "closed_days": []})


def tally_paused(cfg: dict, day: str) -> bool:
    control = control_state(cfg)
    return bool(control.get("maintenance") or day in control.get("closed_days", []))


def save_ledger(ledger: dict) -> None:
    """Persist the ledger, including each row's 'vat' (verified-at) timestamp."""
    save_json(LEDGER_PATH, ledger)


def today_key(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).strftime("%Y-%m-%d")


def prune(ledger: dict) -> None:
    cutoff = (datetime.now(TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for day in [d for d in ledger if d < cutoff]:
        ledger.pop(day, None)


def record(ledger: dict, day: str, entry: dict) -> None:
    rows = ledger.setdefault(day, [])
    for i, row in enumerate(rows):
        if row.get("chat_id") == entry["chat_id"] and row.get("message_id") == entry["message_id"]:
            rows[i] = entry  # edited message replaces the old figures
            return
    rows.append(entry)


def find_reference(
    ledger: dict,
    day: str,
    reference: str,
    exclude_chat_id=None,
    exclude_message_id=None,
) -> dict | None:
    """Find an already-recorded reference for the same local day."""
    if not reference:
        return None
    for row in ledger.get(day, []):
        if (
            row.get("chat_id") == exclude_chat_id
            and row.get("message_id") == exclude_message_id
        ):
            continue
        existing_ref = row.get("reference_number", "")
        if is_reference_match(reference, existing_ref):
            return row
    return None


def remove_message(ledger: dict, chat_id, message_id) -> bool:
    """Remove every existing ledger row for an edited message."""
    removed = False
    for day in list(ledger):
        rows = ledger[day]
        kept = [
            row
            for row in rows
            if not (row.get("chat_id") == chat_id and row.get("message_id") == message_id)
        ]
        if len(kept) != len(rows):
            removed = True
            if kept:
                ledger[day] = kept
            else:
                ledger.pop(day, None)
    return removed


def summarize(ledger: dict, day: str, cfg: dict) -> dict:
    rows = ledger.get(day, [])
    if cfg.get("count_only_owner") and cfg.get("owner_ids"):
        rows = [r for r in rows if r.get("sender_id") in cfg["owner_ids"]]
    counted = [r for r in rows if r.get("total", 0) > 0]
    buckets: dict[int, int] = {}
    for r in counted:
        for a in r.get("amounts", []):
            buckets[a] = buckets.get(a, 0) + 1
    return {
        "day": day,
        "messages": len(counted),
        "total": sum(r["total"] for r in counted),
        "items": sum(buckets.values()),
        "buckets": buckets,
        "stale": sum(
            1
            for r in counted
            if not r.get("vat")
            and r.get("ts", 0) < time.time() - cfg.get("stale_notice_seconds", 900)
        ),
        "rows": counted,
    }
