"""Telegram Bot API communication and background deletion probe."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from src.core.ledger import LEDGER_LOCK, control_state, save_ledger, today_key

API = "https://api.telegram.org/bot{token}/{method}"


def api_call(token: str, method: str, payload: dict, timeout: int = 65) -> dict:
    """POST to the Bot API. Returns the parsed body for both ok and error results."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API.format(token=token, method=method),
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            return {"ok": False, "error_code": exc.code, "description": "unparseable error body"}


def message_exists(token: str, chat_id, message_id) -> bool | None:
    """Is this message still present in the chat?

    Telegram never notifies bots about deletions, so probe instead.
    setMessageReaction with an empty reaction list changes nothing and is
    invisible in the chat, but it does reveal whether the target is gone:

      live message    -> "REACTION_EMPTY"
      deleted message -> "message to react not found"

    Returns True (live), False (deleted), None (unknown - caller keeps the row).
    """
    if not message_id:
        return None
    try:
        resp = api_call(
            token,
            "setMessageReaction",
            {"chat_id": chat_id, "message_id": message_id, "reaction": []},
            timeout=15,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[probe] {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    desc = str(resp.get("description", ""))
    if resp.get("ok") or "REACTION_EMPTY" in desc:
        return True
    if "not found" in desc.lower():
        return False
    print(f"[probe] unexpected: {desc}", file=sys.stderr)
    return None


def _recheck_interval(row: dict, now: float, cfg: dict) -> float:
    age = now - row.get("ts", 0)
    if age < cfg["fresh_window_seconds"]:
        return cfg["recheck_fresh_seconds"]
    return cfg["recheck_old_seconds"]


def _needs_check(row: dict, now: float, cfg: dict) -> bool:
    if now - row.get("ts", 0) < cfg["verify_grace_seconds"]:
        return False  # too fresh; let edits settle
    return now - row.get("vat", 0) >= _recheck_interval(row, now, cfg)


def verify_day(
    token: str,
    ledger: dict,
    day: str,
    cfg: dict,
    budget_seconds: float | None = None,
) -> tuple[int, int]:
    """Probe stale rows in parallel and drop deleted ones.

    Returns (removed, checked). Bounded by budget_seconds; None means no limit.
    """
    now = time.time()
    with LEDGER_LOCK:  # snapshot, so an incoming message can't mutate mid-scan
        rows = list(ledger.get(day) or [])
    if not rows:
        return (0, 0)
    todo = [r for r in rows if _needs_check(r, now, cfg)]
    if not todo:
        return (0, 0)
    # Never-verified rows first, then whatever was verified longest ago.
    todo.sort(key=lambda r: r.get("vat", 0))

    workers = max(1, int(cfg["verify_workers"]))
    deadline = None if budget_seconds is None else now + float(budget_seconds)
    doomed: list[int] = []  # id() of rows to drop
    checked = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(todo), workers):
            if deadline is not None and time.time() >= deadline:
                break
            batch = todo[i : i + workers]
            results = list(
                pool.map(
                    lambda r: message_exists(token, r.get("chat_id"), r.get("message_id")),
                    batch,
                )
            )
            stamp = time.time()
            for row, alive in zip(batch, results):
                checked += 1
                if alive is False:
                    doomed.append(id(row))
                    print(
                        f"[deleted] day={day} msg={row.get('message_id')} "
                        f"total={row.get('total')} dropped",
                        flush=True,
                    )
                elif alive is True:
                    row["vat"] = int(stamp)
                # alive is None -> leave vat alone, retry next pass

    if doomed:
        dead = set(doomed)
        with LEDGER_LOCK:
            keep = [r for r in (ledger.get(day) or []) if id(r) not in dead]
            if keep:
                ledger[day] = keep
            else:
                ledger.pop(day, None)
    return (len(doomed), checked)


def sweep_forever(token: str, ledger: dict, cfg: dict) -> None:
    """Continuously re-probe rows on a background thread.

    Probing costs ~1s per message and Telegram serialises reactions per chat, so
    it must never sit in the command path: a reply that waits on probes feels
    broken. This thread does the work between commands instead, so /total can
    answer straight from the ledger.
    """
    interval = max(1, int(cfg["sweep_interval_seconds"]))
    while True:
        try:
            days = [today_key()]
            with LEDGER_LOCK:
                days += [d for d in sorted(ledger, reverse=True) if d not in days][:1]
            for day in days:
                if day in control_state(cfg).get("closed_days", []):
                    continue  # /dayclose is an immutable snapshot
                removed, checked = verify_day(token, ledger, day, cfg, cfg["sweep_budget_seconds"])
                if removed:
                    with LEDGER_LOCK:
                        save_ledger(ledger)
                if checked:
                    break
        except Exception as exc:  # a sweep failure must not kill the daemon
            print(f"[sweep] {type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(interval)


def chat_action(token: str, chat_id, action: str = "typing") -> None:
    api_call(token, "sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)


def send(
    token: str,
    chat_id,
    text: str,
    reply_to=None,
    thread_id=None,
    reply_markup: dict | None = None,
) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if thread_id:
        payload["message_thread_id"] = thread_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = api_call(token, "sendMessage", payload, timeout=20)
    if not resp.get("ok"):
        print(f"[send] failed: {resp.get('description')}", file=sys.stderr)
    return resp


def answer_callback(token: str, callback_id: str) -> None:
    api_call(token, "answerCallbackQuery", {"callback_query_id": callback_id}, timeout=10)


def edit_list_message(token: str, chat_id, message_id, text: str, markup: dict) -> None:
    resp = api_call(
        token,
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": markup,
        },
        timeout=20,
    )
    if not resp.get("ok") and "message is not modified" not in str(resp.get("description", "")).lower():
        print(f"[edit-list] failed: {resp.get('description')}", file=sys.stderr)
