"""Self-test suite for the tally bot.

Run via `python3 main.py --self-test` (stdlib only, no network required).
"""

from __future__ import annotations

import re
import time

import main as tally_main
from main import handle_message
from src.core.ledger import summarize, tally_paused, today_key
from src.parser.amount_parser import (
    extract_reference,
    is_reference_match,
    label,
    parse_amounts,
    reply_reference,
)
from src.telegram.handlers import (
    _footer,
    handle_callback,
    list_keyboard,
    render_details,
    render_list,
    render_search,
)
import src.telegram.handlers as handlers_mod


def run() -> int:
    cases = [
        ("10K", [10000]),
        ("25k paid", [25000]),
        ("5K + 10K", [5000, 10000]),
        ("1.5K", [1500]),
        ("15,000 MMK", [15000]),
        ("20000", [20000]),
        ("2M", [2000000]),
        ("၁၀K", [10000]),
        ("yes ok", []),
        ("gave 3 items", []),            # bare < 1000 ignored
        ("https://x.com/1000000 check", []),  # url stripped
        ("at 8:30 AM 30K", [30000]),
        ("5K ✅ , 25K ✅, 15K✅", [5000, 25000, 15000]),
        ("5K + 25K + 15K = 45K", [5000, 25000, 15000]),
        ("10K = 10K", [10000]),
        ("09672571794 25K ✅", [25000]),
        ("9672571794 25K", [25000]),
        ("account 1234567 5K", [5000]),
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
        ("09672376152\nBill 25K\nOM", "09672376152"),
        ("09 672 376 152\nBill 25K\nOM", "09672376152"),
        ("9672376152\nBill 25K\nOM", "09672376152"),
        ("035 265", "035265"),
        ("Bill 25K", None),
    ]:
        got = extract_reference(text)
        ok = got == want
        failed += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} reference {text!r} -> {got!r} (want {want!r})")

    # Partial / Substring quote matching: when user highlights partial phone '675362816'
    # from parent '09675362816', reply_reference must resolve to the full '09675362816'
    partial_quote = {
        "quote": {"text": "675362816"},
        "reply_to_message": {"text": "09675362816\nBill 20K"},
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
        "reply_to_message": {"message_id": 7001, "text": "09672376152\nBill 25K"},
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
    real_send = tally_main.send
    tally_main.send = lambda *args, **kwargs: sent_warnings.append({"args": args, "kwargs": kwargs}) or {"ok": True}
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
        tally_main.send = real_send

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
        and out.count("\n") + 1 == 7
    )
    failed += 0 if ok else 1
    print(f"\n{'ok  ' if ok else 'FAIL'} grouped render: 235 rows -> {out.count(chr(10)) + 1} lines")
    print(re.sub(r"</?b>|</?i>", "", out))

    # One message carrying 3 figures -> 1 message but 3 items, so the count is shown.
    multi = [{"total": 45000, "amounts": [5000, 25000, 15000], "ts": 0, "vat": 1}]
    ms = summarize({"2026-08-22": multi}, "2026-08-22", cfg)
    mout = render_details(ms, cfg)
    ok = ms["messages"] == 1 and ms["items"] == 3 and "<b>3</b> items" in mout
    failed += 0 if ok else 1
    print(f"\n{'ok  ' if ok else 'FAIL'} multi-amount footer shows item count")
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
    ok = "09672376152" in hit and "25,000" in hit and "Not found" in miss
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} partial spaced search")

    # Pagination callbacks carry the rendered day so paging a past-day view
    # stays on that day instead of silently switching to today.
    past_day = "2026-08-20"
    today_day = today_key()
    past_rows = [
        {"total": 25000, "amounts": [25000], "ts": 1758000000 + i, "vat": 1, "message_id": i}
        for i in range(25)  # 2 pages at LIST_PAGE_SIZE=20
    ]
    cb_cfg = {"count_only_owner": False, "owner_ids": [], "currency_suffix": ""}
    cb_ledger = {
        past_day: past_rows,
        today_day: [
            {"total": 5000, "amounts": [5000], "ts": now, "vat": 1, "message_id": 999}
        ],
    }
    kb = list_keyboard(summarize(cb_ledger, past_day, cb_cfg), page=1)
    nav_data = [
        b["callback_data"]
        for row in kb["inline_keyboard"]
        for b in row
        if b["callback_data"] != "tally:noop"
    ]
    captured: dict = {}
    real_answer_cb = handlers_mod.answer_callback
    real_edit_list = handlers_mod.edit_list_message
    handlers_mod.answer_callback = lambda token, callback_id: None

    def fake_edit(token, chat_id, message_id, text, markup) -> None:
        captured["text"] = text
        captured["markup"] = markup

    handlers_mod.edit_list_message = fake_edit
    try:
        query_new = {
            "id": "cb1",
            "data": f"tally:list:{past_day}:1",
            "message": {"chat": {"id": -1004417247378}, "message_id": 55},
        }
        handle_callback(query_new, cb_cfg, cb_ledger, "unused")
        new_format_ok = (
            all(d.startswith(f"tally:list:{past_day}:") for d in nav_data)
            and nav_data
            and "2026-08-20 Message Log" in captured.get("text", "")
            and "Page 1/2" in captured.get("text", "")
            and "Total: <b>625,000</b>" in captured.get("text", "")
        )
        # Legacy keyboards (day-less format) fall back to today instead of crashing.
        query_legacy = dict(query_new, id="cb2", data="tally:list:1")
        handle_callback(query_legacy, cb_cfg, cb_ledger, "unused")
        legacy_ok = (
            f"{today_day} Message Log" in captured.get("text", "")
            and "5,000" in captured.get("text", "")
            and "Page 1/1" in captured.get("text", "")
        )
    finally:
        handlers_mod.answer_callback = real_answer_cb
        handlers_mod.edit_list_message = real_edit_list
    failed += 0 if new_format_ok and legacy_ok else 1
    print(f"{'ok  ' if new_format_ok and legacy_ok else 'FAIL'} pagination callbacks stay on rendered day")

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
    tally_main.send = lambda *args, **kwargs: sent_warnings.append({"args": args, "kwargs": kwargs}) or {"ok": True}
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
        tally_main.send = real_send

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
