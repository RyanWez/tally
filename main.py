#!/usr/bin/env python3
"""Telegram group amount tally bot.

Zero-LLM, stdlib-only. Reads group messages, parses amounts like 10K / 25K /
5,000 / 15000, keeps a per-day ledger, and answers /total, /details, /list.

Usage:
  python3 main.py --self-test            parse/format checks, no network
  python3 main.py --run                  long-poll daemon
  python3 main.py --report [DAY]         print a day's tally locally
  python3 main.py --verify [DAY]         full deletion sweep for a day
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import threading
import time
import urllib.error
from datetime import datetime
from pathlib import Path

# Add project root to sys.path if not present
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.core.config import (
    DB_PATH,
    TZ,
    load_config,
)
from src.core.ledger import (
    LEDGER_LOCK,
    Ledger,
    control_state,
    tally_paused,
    today_key,
)
from src.parser.amount_parser import (
    fmt,
    label,
    parse_amounts,
    reply_reference,
)
from src.telegram.client import (
    api_call,
    send,
    sweep_forever,
    verify_day,
)
from src.telegram.handlers import (
    handle_callback,
    handle_command,
    render_details,
)

_SEEN_CHATS: set = set()


def handle_message(msg: dict, cfg: dict, db: Ledger, token: str, edited: bool) -> bool:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    is_private = (chat.get("type") or "") == "private"
    if chat_id not in _SEEN_CHATS:
        _SEEN_CHATS.add(chat_id)
        print(f"[chat] id={chat_id} type={chat.get('type')} title={chat.get('title')!r}", flush=True)

    sender = msg.get("from", {}) or {}
    sender_id = sender.get("id")
    owners = cfg["owner_ids"]

    if is_private:
        # DM: only the owner gets any reply at all. Everyone else is ignored.
        if not owners or sender_id not in owners:
            print(f"[dm-ignored] from={sender_id} @{sender.get('username')}", flush=True)
            return False
    elif cfg["allowed_chat_ids"] and chat_id not in cfg["allowed_chat_ids"]:
        return False

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return False

    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if text.startswith("/") else ""
    if cmd:
        if not is_private and cfg["group_commands"] == "owner" and owners and sender_id not in owners:
            return False
        # /search may contain spaces ("035 265"), so keep the whole argument.
        command_arg = text.split(None, 1)[1].strip() if len(parts) > 1 else ""
        handle_command(cmd, command_arg, msg, cfg, db, token)
        return False

    if is_private:
        return False  # only group messages feed the ledger

    # Only the owner's own messages are tallied.
    if cfg["count_only_owner"] and owners and sender_id not in owners:
        return False

    ts = int(msg.get("edit_date") or msg.get("date") or time.time())
    day = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
    if tally_paused(cfg, day):
        print(
            f"[paused] day={day} msg={msg.get('message_id')} "
            f"maintenance={control_state(cfg).get('maintenance')} "
            f"closed={day in control_state(cfg).get('closed_days', [])}",
            flush=True,
        )
        return False

    amounts = parse_amounts(text, cfg["min_bare_amount"], cfg["max_bare_digits"])
    if not amounts:
        # An edited amount message may become ordinary text. Remove its old
        # ledger row instead of leaving stale money in the day's total.
        if edited:
            removed = db.remove_message(chat_id, msg.get("message_id"))
            if removed:
                print(
                    f"[edited-no-amount] chat={chat_id} msg={msg.get('message_id')} removed",
                    flush=True,
                )
            return removed
        return False

    # 1. Denomination & Allowed Amount Validation (5K, 10K, 15K, 20K, 25K)
    allowed_denoms = cfg.get("allowed_denominations", [5000, 10000, 15000, 20000, 25000])
    strict = cfg.get("strict_denominations", True)
    min_allowed = cfg.get("min_allowed_amount", 5000)
    max_allowed = cfg.get("max_allowed_amount", 25000)

    invalid_amounts: list[int] = []
    for a in amounts:
        if strict:
            if a not in allowed_denoms:
                invalid_amounts.append(a)
        else:
            if a < min_allowed or a > max_allowed:
                invalid_amounts.append(a)

    if invalid_amounts:
        if edited:
            db.remove_message(chat_id, msg.get("message_id"))
        bad_str = ", ".join(fmt(a, cfg.get("currency_suffix", "")) for a in invalid_amounts)
        allowed_str = ", ".join(label(x) for x in allowed_denoms)
        send(
            token,
            chat_id,
            f"⚠️ Invalid amount. Allowed amounts: {allowed_str} (min {fmt(min_allowed)}, max {fmt(max_allowed)}).\n"
            f"The amount <b>{bad_str}</b> was not recorded.",
            reply_to=msg.get("message_id"),
        )
        print(
            f"[invalid-amount] day={day} msg={msg.get('message_id')} invalid={invalid_amounts} ignored",
            flush=True,
        )
        return False

    # 2. Require Reply to a valid reference message
    reply = msg.get("reply_to_message") or {}
    reference = reply_reference(msg)

    if cfg.get("require_reply", True) and (not reply or not reference):
        if edited:
            db.remove_message(chat_id, msg.get("message_id"))
        send(
            token,
            chat_id,
            "⚠️ To record an amount, please <b>Reply</b> to the corresponding Phone / Reference message.",
            reply_to=msg.get("message_id"),
        )
        print(
            f"[require-reply] day={day} msg={msg.get('message_id')} missing reply/reference ignored",
            flush=True,
        )
        return False

    quote = msg.get("quote")
    reply_text = (reply.get("text") or reply.get("caption") or "").strip()
    quote_text = (quote.get("text") or "").strip() if isinstance(quote, dict) else None
    day = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
    entry = {
        "chat_id": chat_id,
        "message_id": msg.get("message_id"),
        "ts": ts,
        "sender_id": sender_id,
        "sender_name": sender.get("first_name") or sender.get("username") or str(sender_id),
        "amounts": amounts,
        "total": sum(amounts),
        "edited": bool(edited),
        "vat": 0,  # unverified; the sweep confirms it still exists
        "reply_to_message_id": reply.get("message_id"),
        "reference_number": reference,
        "original_text": text,
        "reply_text": reply_text or None,
        "quote_text": quote_text,
    }

    # 3. Duplicate Prevention (One reference counted once per local day)
    if reference:
        removed_edited_row = False
        with LEDGER_LOCK:  # keep duplicate-check and record atomic
            duplicate = db.find_reference(
                day,
                reference,
                exclude_chat_id=chat_id,
                exclude_message_id=msg.get("message_id"),
            )
            if duplicate:
                if edited:
                    removed_edited_row = db.remove_message(chat_id, msg.get("message_id"))
            else:
                db.record(day, entry)
        if duplicate:
            duplicate_clock = datetime.fromtimestamp(duplicate["ts"], TZ).strftime("%H:%M")
            duplicate_amount = fmt(duplicate["total"], cfg.get("currency_suffix", ""))
            dup_ref = duplicate.get("reference_number") or reference
            send(
                token,
                chat_id,
                f"⚠️ {dup_ref} was already recorded at {duplicate_clock} for {duplicate_amount}. "
                "This message will not be counted again.",
                reply_to=msg.get("message_id"),
            )
            print(
                f"[duplicate] day={day} ref={reference} dup_ref={dup_ref} existing_msg={duplicate.get('message_id')} "
                f"new_msg={msg.get('message_id')} ignored",
                flush=True,
            )
            return removed_edited_row
    else:
        db.record(day, entry)
    return True


def run(cfg: dict) -> int:
    token = cfg["bot_token"]
    if not token:
        print("no bot token configured (use .env, TALLY_BOT_TOKEN or config.json)", file=sys.stderr)
        return 2
    db = Ledger(DB_PATH)  # commits are immediate; no deferred save step
    cfg["_control"] = db.load_control()
    offset = db.get_offset()
    backoff = 1
    offset_dirty = False
    last_prune_day: str | None = None
    stopping = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        # Raising breaks out of a blocking long-poll immediately; the flag
        # covers the moments between syscalls.
        if stopping.is_set():
            raise KeyboardInterrupt
        stopping.set()
        print(f"[shutdown] signal {signum} received, saving state...", flush=True)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print(f"[tally] started, offset={offset}, chats={cfg['allowed_chat_ids']}", flush=True)
    # Verification runs off the command path so replies stay instant.
    threading.Thread(target=sweep_forever, args=(token, db, cfg), daemon=True).start()

    try:
        while not stopping.is_set():
            try:
                resp = api_call(
                    token,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": int(cfg["poll_timeout"]),
                        "allowed_updates": ["message", "edited_message", "callback_query"],
                    },
                )
                backoff = 1
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"[poll] {type(exc).__name__}: {exc}", file=sys.stderr)
                time.sleep(min(backoff, 60))
                backoff *= 2
                continue

            for upd in resp.get("result", []):
                if upd["update_id"] + 1 > offset:
                    offset = upd["update_id"] + 1
                    offset_dirty = True
                if upd.get("callback_query"):
                    try:
                        handle_callback(upd["callback_query"], cfg, db, token)
                    except Exception as exc:
                        print(f"[callback] {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                try:
                    handle_message(msg, cfg, db, token, "edited_message" in upd)
                except Exception as exc:  # keep the daemon alive
                    print(f"[handle] {type(exc).__name__}: {exc}", file=sys.stderr)

            if offset_dirty:  # skip rewriting an unchanged offset every poll
                db.set_offset(offset)
                offset_dirty = False
            today = today_key()
            if today != last_prune_day:  # roll retention once per local day
                db.prune()
                last_prune_day = today
    except KeyboardInterrupt:
        pass
    finally:
        # Persist whatever we saw so a restart neither reprocesses old updates
        # nor loses recorded amounts.
        with LEDGER_LOCK:
            try:
                db.prune()
            finally:
                db.set_offset(offset)
                db.close()
        print("[tally] stopped cleanly, state saved", flush=True)
    return 0


def self_test() -> int:
    """Run the stdlib-only test suite; lives in tests/ to keep main.py lean."""
    from tests.test_main import run as run_tests

    return run_tests()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report", nargs="?", const="", metavar="YYYY-MM-DD")
    ap.add_argument("--verify", nargs="?", const="", metavar="YYYY-MM-DD")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    cfg = load_config()
    if args.verify is not None:
        day = args.verify or today_key()
        with Ledger(DB_PATH) as db:
            removed, checked = verify_day(cfg["bot_token"], db, day, cfg, budget_seconds=None)
        print(f"{day}: checked {checked}, removed {removed}")
        return 0
    if args.report is not None:
        day = args.report or today_key()
        with Ledger(DB_PATH) as db:
            s = db.summarize(day, cfg)
        print(re.sub(r"</?b>|</?i>", "", render_details(s, cfg)))
        return 0
    if args.run:
        return run(cfg)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
