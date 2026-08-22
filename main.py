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
import os
import re
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
    BASE,
    CONFIG_PATH,
    CONTROL_PATH,
    ENV_PATH,
    KEEP_DAYS,
    LEDGER_PATH,
    OFFSET_PATH,
    STATE,
    TZ,
    load_config,
    load_dotenv,
    load_json,
    save_json,
)
from src.core.ledger import (
    LEDGER_LOCK,
    control_state,
    find_reference,
    load_control,
    prune,
    record,
    remove_message,
    save_control,
    save_ledger,
    summarize,
    tally_paused,
    today_key,
)
from src.parser.amount_parser import (
    AMOUNT_RE,
    MY_DIGITS,
    REFERENCE_RE,
    URL_RE,
    extract_reference,
    fmt,
    is_reference_match,
    label,
    normalize_phone_reference,
    normalize_search,
    parse_amounts,
    reply_reference,
)
from src.telegram.client import (
    API,
    answer_callback,
    api_call,
    chat_action,
    edit_list_message,
    message_exists,
    send,
    sweep_forever,
    verify_day,
)
from src.telegram.handlers import (
    HELP,
    _footer,
    handle_callback,
    handle_command,
    list_keyboard,
    list_page_count,
    render_details,
    render_list,
    render_search,
    render_total,
)

_SEEN_CHATS: set = set()


def handle_message(msg: dict, cfg: dict, ledger: dict, token: str, edited: bool) -> bool:
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
        handle_command(cmd, command_arg, msg, cfg, ledger, token)
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
            with LEDGER_LOCK:
                removed = remove_message(ledger, chat_id, msg.get("message_id"))
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
            with LEDGER_LOCK:
                remove_message(ledger, chat_id, msg.get("message_id"))
        bad_str = ", ".join(fmt(a, cfg.get("currency_suffix", "")) for a in invalid_amounts)
        allowed_str = ", ".join(label(x) for x in allowed_denoms)
        send(
            token,
            chat_id,
            f"⚠️ စာမှားပို့မိတာများလား ? အနည်းဆုံး 5K မှ အများဆုံး 25K ({allowed_str}) ပဲ ရှိသင့်တာနော်။\n"
            f"အခု <b>{bad_str}</b> ပမာဏကိုတော့ မှတ်မထားပါဘူးခင်ဗျာ။",
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
            with LEDGER_LOCK:
                remove_message(ledger, chat_id, msg.get("message_id"))
        send(
            token,
            chat_id,
            "⚠️ ပိုက်ဆံစာရင်း မှတ်သားရန် သက်ဆိုင်ရာ Phone / Reference message ကို <b>Reply</b> လုပ်ပြီး ပို့ပေးပါခင်ဗျာ။",
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
        with LEDGER_LOCK:
            duplicate = find_reference(
                ledger,
                day,
                reference,
                exclude_chat_id=chat_id,
                exclude_message_id=msg.get("message_id"),
            )
            if duplicate:
                if edited:
                    removed_edited_row = remove_message(ledger, chat_id, msg.get("message_id"))
            else:
                record(ledger, day, entry)
        if duplicate:
            duplicate_clock = datetime.fromtimestamp(duplicate["ts"], TZ).strftime("%H:%M")
            duplicate_amount = fmt(duplicate["total"], cfg.get("currency_suffix", ""))
            dup_ref = duplicate.get("reference_number") or reference
            send(
                token,
                chat_id,
                f"⚠️ {duplicate_clock} မှာ {dup_ref} ကို {duplicate_amount} နဲ့ "
                "တစ်ကြိမ် မှတ်ထားပြီးသားပါ။ ဒီ message ကို ထပ်မမှတ်ပါ။",
                reply_to=msg.get("message_id"),
            )
            print(
                f"[duplicate] day={day} ref={reference} dup_ref={dup_ref} existing_msg={duplicate.get('message_id')} "
                f"new_msg={msg.get('message_id')} ignored",
                flush=True,
            )
            return removed_edited_row
    else:
        with LEDGER_LOCK:
            record(ledger, day, entry)
    return True


def run(cfg: dict) -> int:
    token = cfg["bot_token"]
    if not token:
        print("no bot token configured (use .env, TALLY_BOT_TOKEN or config.json)", file=sys.stderr)
        return 2
    ledger = load_json(LEDGER_PATH, {})
    cfg["_control"] = load_control()
    offset = load_json(OFFSET_PATH, {}).get("offset", 0)
    backoff = 1
    dirty = False
    print(f"[tally] started, offset={offset}, chats={cfg['allowed_chat_ids']}", flush=True)
    # Verification runs off the command path so replies stay instant.
    threading.Thread(target=sweep_forever, args=(token, ledger, cfg), daemon=True).start()

    while True:
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
            offset = max(offset, upd["update_id"] + 1)
            if upd.get("callback_query"):
                try:
                    handle_callback(upd["callback_query"], cfg, ledger, token)
                except Exception as exc:
                    print(f"[callback] {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            try:
                if handle_message(msg, cfg, ledger, token, "edited_message" in upd):
                    dirty = True
            except Exception as exc:  # keep the daemon alive
                print(f"[handle] {type(exc).__name__}: {exc}", file=sys.stderr)

        if dirty:
            with LEDGER_LOCK:
                prune(ledger)
                save_ledger(ledger)
            dirty = False
        save_json(OFFSET_PATH, {"offset": offset})


def self_test() -> int:
    cases = [
        ("10K", [10000]),
        ("25k ပေးလိုက်ပြီ", [25000]),
        ("5K + 10K", [5000, 10000]),
        ("1.5K", [1500]),
        ("15,000 ကျပ်", [15000]),
        ("20000", [20000]),
        ("2M", [2000000]),
        ("၁၀K", [10000]),
        ("ဟုတ်ကဲ့ ok", []),
        ("3 ခု ပေးလိုက်တယ်", []),            # bare < 1000 ignored
        ("https://x.com/1000000 ကြည့်", []),  # url stripped
        ("မနက် 8:30 မှာ 30K", [30000]),
        ("5K ✅ , 25K ✅, 15K✅", [5000, 25000, 15000]),
        ("5K + 25K + 15K = 45K", [5000, 25000, 15000]),
        ("10K = 10K", [10000]),
        ("09672571794 ကို 25K ✅", [25000]),
        ("9672571794 25K", [25000]),
        ("အကောင် 1234567 ကို 5K", [5000]),
        ("999999", [999999]),
    ]
    failed = 0
    for text, want in cases:
        got = parse_amounts(text)
        ok = got == want
        failed += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} parse {text!r} -> {got} (want {want})")

    for value, want in [
        (5000, "5K"),
        (25000, "25K"),
        (1500, "1.5K"),
        (2000000, "2M"),
        (1500000, "1.5M"),
        (999999, "999,999"),
        (450, "450"),
    ]:
        got = label(value)
        ok = got == want
        failed += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} label {value} -> {got!r} (want {want!r})")

    for text, want in [
        ("09672376152\nဘေ 25K\nOM", "09672376152"),
        ("09 672 376 152\nဘေ 25K\nOM", "09672376152"),
        ("9672376152\nဘေ 25K\nOM", "09672376152"),
        ("035 265", "035265"),
        ("ဘေ 25K", None),
    ]:
        got = extract_reference(text)
        ok = got == want
        failed += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} reference {text!r} -> {got!r} (want {want!r})")

    # Partial / Substring quote matching: when user highlights partial phone '675362816'
    # from parent '09675362816', reply_reference must resolve to the full '09675362816'
    partial_quote = {
        "quote": {"text": "675362816"},
        "reply_to_message": {"text": "09675362816\nဘေ 20K"},
    }
    short_quote = {
        "quote": {"text": "5362816"},
        "reply_to_message": {"text": "09675362816"},
    }
    ok = (
        reply_reference(partial_quote) == "09675362816"
        and reply_reference(short_quote) == "09675362816"
        and is_reference_match("675362816", "09675362816")
        and is_reference_match("5362816", "09675362816")
    )
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} partial quote & phone normalization matching")

    # Store original/reply/quote text and remove an edited row when its amount
    # disappears from the edited message.
    edit_cfg = {
        "allowed_chat_ids": [-1004417247378], "owner_ids": [8777968077],
        "count_only_owner": True, "min_bare_amount": 1000, "max_bare_digits": 6,
        "require_reply": True,
        "allowed_denominations": [5000, 10000, 15000, 20000, 25000],
        "strict_denominations": True,
    }
    edit_ledger: dict = {}
    edit_msg = {
        "chat": {"id": -1004417247378, "type": "supergroup"},
        "from": {"id": 8777968077, "first_name": "owner"},
        "message_id": 9001, "date": int(time.time()), "text": "25K ✅",
        "quote": {"text": "09672376152"},
        "reply_to_message": {"message_id": 7001, "text": "09672376152\nဘေ 25K"},
    }
    recorded = handle_message(edit_msg, edit_cfg, edit_ledger, "unused", False)
    stored = next(iter(edit_ledger.values()))[0]
    edit_msg["text"] = "ok"
    removed = handle_message(edit_msg, edit_cfg, edit_ledger, "unused", True)
    ok = (
        recorded and stored["original_text"] == "25K ✅"
        and stored["quote_text"] == "09672376152"
        and stored["reply_text"].startswith("09672376152")
        and removed and not edit_ledger
    )
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} stored text + edited-to-no-amount removal")

    # Denominations Check: 200K, 10000K, 1K -> REJECTED with warning
    invalid_test_ledger: dict = {}
    sent_warnings: list[dict] = []
    real_send = globals()["send"]
    globals()["send"] = lambda *args, **kwargs: sent_warnings.append({"args": args, "kwargs": kwargs}) or {"ok": True}
    try:
        # 1. 200K is rejected (>25K)
        msg_200k = dict(edit_msg, message_id=9002, text="200K")
        res_200k = handle_message(msg_200k, edit_cfg, invalid_test_ledger, "unused", False)
        # 2. 10000K is rejected (>25K)
        msg_10000k = dict(edit_msg, message_id=9003, text="10000K")
        res_10000k = handle_message(msg_10000k, edit_cfg, invalid_test_ledger, "unused", False)
        # 3. Non-reply 20K is rejected
        non_reply_msg = {
            "chat": {"id": -1004417247378, "type": "supergroup"},
            "from": {"id": 8777968077, "first_name": "owner"},
            "message_id": 9004, "date": int(time.time()), "text": "20K",
        }
        res_non_reply = handle_message(non_reply_msg, edit_cfg, invalid_test_ledger, "unused", False)
    finally:
        globals()["send"] = real_send

    ok = (
        not res_200k and not res_10000k and not res_non_reply
        and len(invalid_test_ledger) == 0
        and len(sent_warnings) == 3
        and "200,000" in sent_warnings[0]["args"][2]
        and "10,000,000" in sent_warnings[1]["args"][2]
        and "Reply" in sent_warnings[2]["args"][2]
    )
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} denomination limits (>25K reject) & require-reply enforcement")

    # Grouped rendering with a realistic 300+ message day.
    cfg = {"count_only_owner": False, "owner_ids": [], "currency_suffix": ""}
    rows = []
    for amount, count in ((25000, 200), (10000, 25), (5000, 10)):
        for i in range(count):
            rows.append({"total": amount, "amounts": [amount], "ts": 0, "vat": 1, "message_id": i})
    s = summarize({"2026-08-22": rows}, "2026-08-22", cfg)
    out = render_details(s, cfg)
    expect_total = 200 * 25000 + 25 * 10000 + 10 * 5000
    ok = (
        s["total"] == expect_total
        and s["messages"] == 235
        and s["items"] == 235
        and s["stale"] == 0
        and "ပိုက်ဆံ" not in out
        and out.count("\n") + 1 == 7
    )
    failed += 0 if ok else 1
    print(f"\n{'ok  ' if ok else 'FAIL'} grouped render: 235 rows -> {out.count(chr(10)) + 1} lines")
    print(re.sub(r"</?b>|</?i>", "", out))

    # One message carrying 3 figures -> 1 စောင် but 3 ခု, so the count is shown.
    multi = [{"total": 45000, "amounts": [5000, 25000, 15000], "ts": 0, "vat": 1}]
    ms = summarize({"2026-08-22": multi}, "2026-08-22", cfg)
    mout = render_details(ms, cfg)
    ok = ms["messages"] == 1 and ms["items"] == 3 and "ပိုက်ဆံ" in mout
    failed += 0 if ok else 1
    print(f"\n{'ok  ' if ok else 'FAIL'} multi-amount footer shows ပိုက်ဆံ count")
    print(re.sub(r"</?b>|</?i>", "", mout))

    # A just-arrived unverified row must stay silent; only long-stale rows warn.
    now = int(time.time())
    fresh = summarize({"d": [{"total": 25000, "amounts": [25000], "ts": now, "vat": 0}]}, "d", cfg)
    aged = summarize(
        {"d": [{"total": 25000, "amounts": [25000], "ts": now - 7200, "vat": 0}]}, "d", cfg
    )
    ok = fresh["stale"] == 0 and _footer(fresh, cfg) == "" and aged["stale"] == 1
    failed += 0 if ok else 1
    print(f"\n{'ok  ' if ok else 'FAIL'} fresh row silent, 2h-unverified row warns")

    page_rows = [
        {"total": 25000, "amounts": [25000], "ts": i, "vat": 1, "message_id": i}
        for i in range(100)
    ]
    ps = summarize({"d": page_rows}, "d", cfg)
    p1 = render_list(ps, cfg, page=1)
    p3 = render_list(ps, cfg, page=3)
    ok = "Page 1/5" in p1 and "Page 5/5" in render_list(ps, cfg, page=5) and "100." in p1 and "1." in render_list(ps, cfg, page=5)
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} list pagination: 100 rows -> 5 pages")

    search_ledger = {
        "d": [
            {"total": 25000, "amounts": [25000], "ts": 2, "reference_number": "09672376152"},
            {"total": 5000, "amounts": [5000], "ts": 1, "reference_number": "035265"},
        ]
    }
    hit = render_search(search_ledger, "672 376", cfg)
    miss = render_search(search_ledger, "12345", cfg)
    ok = "09672376152" in hit and "25,000" in hit and "မတွေ့ဘူး" in miss
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} partial spaced search")

    # A reference may be counted only once per local day (including sub-part quotes).
    duplicate_ledger: dict = {}
    base_msg = {
        "chat": {"id": -1004417247378, "type": "supergroup"},
        "from": {"id": 8777968077, "first_name": "owner"},
        "message_id": 9101, "date": int(time.time()), "text": "25K ✅",
        "quote": {"text": "675362816"},
        "reply_to_message": {"message_id": 7101, "text": "09675362816"},
    }
    sent_warnings = []
    globals()["send"] = lambda *args, **kwargs: sent_warnings.append({"args": args, "kwargs": kwargs}) or {"ok": True}
    try:
        first = handle_message(base_msg, edit_cfg, duplicate_ledger, "unused", False)
        # Sub-part quote or full phone quote in duplicate message
        duplicate_msg = {
            "chat": {"id": -1004417247378, "type": "supergroup"},
            "from": {"id": 8777968077, "first_name": "owner"},
            "message_id": 9102, "date": int(time.time()), "text": "20K ✅",
            "quote": {"text": "09675362816"},
            "reply_to_message": {"message_id": 7101, "text": "09675362816"},
        }
        second = handle_message(duplicate_msg, edit_cfg, duplicate_ledger, "unused", False)
    finally:
        globals()["send"] = real_send

    dup_rows = next(iter(duplicate_ledger.values()))
    warning_text = sent_warnings[0]["args"][2] if sent_warnings else ""
    ok = (
        first and not second and len(dup_rows) == 1 and dup_rows[0]["total"] == 25000
        and "09675362816" in warning_text and "25,000" in warning_text
        and sent_warnings[0]["kwargs"].get("reply_to") == 9102
    )
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} same-day duplicate blocked across sub-part quotes")

    # Control-state semantics: dayclose affects only that local day; maintenance
    # pauses every day. Both are persistent state, not transient process flags.
    control_cfg = {"_control": {"maintenance": False, "closed_days": ["2026-08-22"]}}
    ok = (
        tally_paused(control_cfg, "2026-08-22")
        and not tally_paused(control_cfg, "2026-08-23")
    )
    control_cfg["_control"]["maintenance"] = True
    ok = ok and tally_paused(control_cfg, "2026-08-23")
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} dayclose/maintenance pause semantics")

    print(f"\n{'all passed' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


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
        ledger = load_json(LEDGER_PATH, {})
        removed, checked = verify_day(cfg["bot_token"], ledger, day, cfg, budget_seconds=None)
        save_ledger(ledger)
        print(f"{day}: checked {checked}, removed {removed}")
        return 0
    if args.report is not None:
        ledger = load_json(LEDGER_PATH, {})
        day = args.report or today_key()
        s = summarize(ledger, day, cfg)
        print(re.sub(r"</?b>|</?i>", "", render_details(s, cfg)))
        return 0
    if args.run:
        return run(cfg)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
