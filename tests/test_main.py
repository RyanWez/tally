"""Self-test suite for the tally bot.

Run via `python3 main.py --self-test` (stdlib only, no network required).
SQLite-backed tests use throwaway databases under /tmp/opencode.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

import main as tally_main
from main import handle_message
from src.core.ledger import Ledger, tally_paused, today_key
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

_TMP_ROOT = Path("/tmp/opencode")


def make_db(name: str) -> Ledger:
    """Fresh throwaway database for one test section (no legacy import)."""
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"tally-{name}-", dir=_TMP_ROOT))
    return Ledger(tmp / "tally.db", import_legacy=False)


_SEED_COUNTER = {"next": 0}


def seed(db: Ledger, day: str, rows: list[dict], chat_id: int = -1004417247378) -> None:
    """Insert fabricated rows; message_ids auto-increment to avoid PK clashes."""
    for r in rows:
        _SEED_COUNTER["next"] += 1
        db.record(
            day,
            {
                "chat_id": r.get("chat_id", chat_id),
                "message_id": r.get("message_id", _SEED_COUNTER["next"]),
                "ts": r.get("ts", 0),
                "sender_id": r.get("sender_id"),
                "sender_name": r.get("sender_name"),
                "amounts": r.get("amounts", [r.get("total", 0)]),
                "total": r.get("total", 0),
                "edited": bool(r.get("edited")),
                "vat": r.get("vat", 1),
                "reply_to_message_id": r.get("reply_to_message_id"),
                "reference_number": r.get("reference_number"),
                "original_text": r.get("original_text"),
                "reply_text": r.get("reply_text"),
                "quote_text": r.get("quote_text"),
            },
        )


def total_rows(db: Ledger) -> int:
    return sum(db.count_rows(day) for day in db.day_keys())


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
    edit_db = make_db("edited")
    edit_msg = {
        "chat": {"id": -1004417247378, "type": "supergroup"},
        "from": {"id": 8777968077, "first_name": "owner"},
        "message_id": 9001, "date": int(time.time()), "text": "25K ✅",
        "quote": {"text": "09672376152"},
        "reply_to_message": {"message_id": 7001, "text": "09672376152\nBill 25K"},
    }
    recorded = handle_message(edit_msg, edit_cfg, edit_db, "unused", False)
    stored = edit_db.rows_for_day(edit_db.day_keys()[0])[0]
    edit_msg["text"] = "ok"
    removed = handle_message(edit_msg, edit_cfg, edit_db, "unused", True)
    ok = (
        recorded and stored["original_text"] == "25K ✅"
        and stored["quote_text"] == "09672376152"
        and stored["reply_text"].startswith("09672376152")
        and removed and total_rows(edit_db) == 0
    )
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} stored text + edited-to-no-amount removal (sqlite)")

    # Denominations Check: 200K, 10000K, 1K -> REJECTED with warning
    invalid_db: Ledger = make_db("invalid")
    sent_warnings: list[dict] = []
    real_send = tally_main.send
    tally_main.send = lambda *args, **kwargs: sent_warnings.append({"args": args, "kwargs": kwargs}) or {"ok": True}
    try:
        # 1. 200K is rejected (>25K)
        msg_200k = dict(edit_msg, message_id=9002, text="200K")
        res_200k = handle_message(msg_200k, edit_cfg, invalid_db, "unused", False)
        # 2. 10000K is rejected (>25K)
        msg_10000k = dict(edit_msg, message_id=9003, text="10000K")
        res_10000k = handle_message(msg_10000k, edit_cfg, invalid_db, "unused", False)
        # 3. Non-reply 20K is rejected
        non_reply_msg = {
            "chat": {"id": -1004417247378, "type": "supergroup"},
            "from": {"id": 8777968077, "first_name": "owner"},
            "message_id": 9004, "date": int(time.time()), "text": "20K",
        }
        res_non_reply = handle_message(non_reply_msg, edit_cfg, invalid_db, "unused", False)
    finally:
        tally_main.send = real_send

    ok = (
        not res_200k and not res_10000k and not res_non_reply
        and total_rows(invalid_db) == 0
        and len(sent_warnings) == 3
        and "200,000" in sent_warnings[0]["args"][2]
        and "10,000,000" in sent_warnings[1]["args"][2]
        and "Reply" in sent_warnings[2]["args"][2]
    )
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} denomination limits (>25K reject) & require-reply enforcement")

    # Grouped rendering with a realistic 300+ message day.
    cfg = {"count_only_owner": False, "owner_ids": [], "currency_suffix": ""}
    grouped_db = make_db("grouped")
    rows = []
    for amount, count in ((25000, 200), (10000, 25), (5000, 10)):
        for i in range(count):
            # message_id omitted -> seed() assigns unique PKs across groups
            rows.append({"total": amount, "amounts": [amount], "ts": 0, "vat": 1})
    seed(grouped_db, "2026-08-22", rows)
    s = grouped_db.summarize("2026-08-22", cfg)
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
    multi_db = make_db("multi")
    seed(multi_db, "2026-08-22", [
        {"total": 45000, "amounts": [5000, 25000, 15000], "ts": 0, "vat": 1, "message_id": 1}
    ])
    ms = multi_db.summarize("2026-08-22", cfg)
    mout = render_details(ms, cfg)
    ok = ms["messages"] == 1 and ms["items"] == 3 and "<b>3</b> items" in mout
    failed += 0 if ok else 1
    print(f"\n{'ok  ' if ok else 'FAIL'} multi-amount footer shows item count")
    print(re.sub(r"</?b>|</?i>", "", mout))

    # A just-arrived unverified row must stay silent; only long-stale rows warn.
    now = int(time.time())
    stale_db = make_db("stale")
    seed(stale_db, "fresh", [{"total": 25000, "amounts": [25000], "ts": now, "vat": 0, "message_id": 1}])
    seed(stale_db, "aged", [{"total": 25000, "amounts": [25000], "ts": now - 7200, "vat": 0, "message_id": 2}])
    fresh = stale_db.summarize("fresh", cfg)
    aged = stale_db.summarize("aged", cfg)
    ok = fresh["stale"] == 0 and _footer(fresh, cfg) == "" and aged["stale"] == 1
    failed += 0 if ok else 1
    print(f"\n{'ok  ' if ok else 'FAIL'} fresh row silent, 2h-unverified row warns")

    page_db = make_db("pages")
    page_rows = [
        {"total": 25000, "amounts": [25000], "ts": i, "vat": 1, "message_id": i}
        for i in range(100)
    ]
    seed(page_db, "d", page_rows)
    ps = page_db.summarize("d", cfg)
    p1 = render_list(ps, cfg, page=1)
    p5 = render_list(ps, cfg, page=5)
    ok = "Page 1/5" in p1 and "Page 5/5" in p5 and "100." in p1 and "1." in p5
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} list pagination: 100 rows -> 5 pages")

    search_db = make_db("search")
    seed(search_db, "d", [
        {"total": 25000, "amounts": [25000], "ts": 2, "vat": 1, "message_id": 11,
         "reference_number": "09672376152"},
        {"total": 5000, "amounts": [5000], "ts": 1, "vat": 1, "message_id": 12,
         "reference_number": "035265"},
    ])
    hit = render_search(search_db, "672 376", cfg)
    miss = render_search(search_db, "12345", cfg)
    ok = "09672376152" in hit and "25,000" in hit and "Not found" in miss
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} partial spaced search")

    # Pagination callbacks carry the rendered day so paging a past-day view
    # stays on that day instead of silently switching to today.
    past_day = "2026-08-20"
    today_day = today_key()
    cb_cfg = {"count_only_owner": False, "owner_ids": [], "currency_suffix": ""}
    cb_db = make_db("callback")
    seed(cb_db, past_day, [
        {"total": 25000, "amounts": [25000], "ts": 1758000000 + i, "vat": 1, "message_id": i}
        for i in range(25)  # 2 pages at LIST_PAGE_SIZE=20
    ])
    seed(cb_db, today_day, [
        {"total": 5000, "amounts": [5000], "ts": now, "vat": 1, "message_id": 999}
    ])
    kb = list_keyboard(cb_db.summarize(past_day, cb_cfg), page=1)
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
        handle_callback(query_new, cb_cfg, cb_db, "unused")
        new_format_ok = (
            all(d.startswith(f"tally:list:{past_day}:") for d in nav_data)
            and nav_data
            and "2026-08-20 Message Log" in captured.get("text", "")
            and "Page 1/2" in captured.get("text", "")
            and "Total: <b>625,000</b>" in captured.get("text", "")
        )
        # Legacy keyboards (day-less format) fall back to today instead of crashing.
        query_legacy = dict(query_new, id="cb2", data="tally:list:1")
        handle_callback(query_legacy, cb_cfg, cb_db, "unused")
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

    # SQLite persistence: rows survive reopen; offset & control round-trip;
    # an edit that crosses midnight keeps exactly ONE row (under the new day).
    persist_dir = _TMP_ROOT / f"tally-persist-{int(time.time()*1000)}"
    persist_dir.mkdir(parents=True, exist_ok=True)
    pdb_path = persist_dir / "tally.db"
    base_entry = {
        "chat_id": -1004417247378, "message_id": 77, "ts": 1758000000,
        "sender_id": 8777968077, "sender_name": "owner",
        "amounts": [25000], "total": 25000, "edited": False, "vat": 0,
        "reply_to_message_id": 70, "reference_number": "09675362816",
        "original_text": "25k", "reply_text": "09675362816", "quote_text": None,
    }
    with Ledger(pdb_path, import_legacy=False) as pdb:
        pdb.record("2026-08-20", base_entry)
        pdb.set_offset(424242)
        pdb.save_control({"maintenance": True, "closed_days": ["2026-08-19"]})
        pdb.record("2026-08-21", dict(base_entry, ts=1758086400))  # midnight-crossing edit
        moved_ok = total_rows(pdb) == 1 and pdb.day_keys() == ["2026-08-21"]
    with Ledger(pdb_path, import_legacy=False) as pdb2:
        s2 = pdb2.summarize("2026-08-21", cfg)
        ctl = pdb2.load_control()
        ok = (
            moved_ok
            and s2["messages"] == 1 and s2["total"] == 25000
            and s2["rows"][0]["reference_number"] == "09675362816"
            and pdb2.get_offset() == 424242
            and ctl["maintenance"] is True
            and ctl["closed_days"] == ["2026-08-19"]
        )
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} sqlite persistence + offset/control roundtrip + midnight upsert")

    # Legacy JSON auto-import: existing state files are migrated on first open
    # and left untouched as backup.
    mig_dir = _TMP_ROOT / f"tally-migrate-{int(time.time()*1000)}"
    mig_dir.mkdir(parents=True, exist_ok=True)
    legacy_ledger = {
        "2026-08-20": [{
            "chat_id": -1004417247378, "message_id": 33, "ts": 1758000000,
            "sender_id": 8777968077, "sender_name": "Ryan Wez",
            "amounts": [25000], "total": 25000, "edited": False, "vat": 1758009956,
            "reply_to_message_id": 2, "reference_number": "09675362816",
            "original_text": "25k", "reply_text": "09675362816", "quote_text": None,
        }]
    }
    (mig_dir / "ledger.json").write_text(json.dumps(legacy_ledger), encoding="utf-8")
    (mig_dir / "offset.json").write_text(json.dumps({"offset": 52623308}), encoding="utf-8")
    (mig_dir / "control.json").write_text(
        json.dumps({"maintenance": False, "closed_days": ["2026-08-01"]}), encoding="utf-8"
    )

    import src.core.ledger as ledger_mod
    real_paths = (ledger_mod.LEDGER_PATH, ledger_mod.OFFSET_PATH, ledger_mod.CONTROL_PATH)
    ledger_mod.LEDGER_PATH = mig_dir / "ledger.json"
    ledger_mod.OFFSET_PATH = mig_dir / "offset.json"
    ledger_mod.CONTROL_PATH = mig_dir / "control.json"
    try:
        mdb = Ledger(mig_dir / "tally.db", import_legacy=True)
        try:
            found = mdb.find_reference("2026-08-20", "675362816")
            ctl = mdb.load_control()
            ok = (
                mdb.count_rows("2026-08-20") == 1
                and found is not None and found["total"] == 25000
                and mdb.get_offset() == 52623308
                and ctl["closed_days"] == ["2026-08-01"]
                and (mig_dir / "ledger.json").exists()  # originals kept as backup
            )
        finally:
            mdb.close()
    finally:
        ledger_mod.LEDGER_PATH, ledger_mod.OFFSET_PATH, ledger_mod.CONTROL_PATH = real_paths
    failed += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} legacy JSON auto-import (ledger+offset+control, backup intact)")

    # A reference may be counted only once per local day (including sub-part quotes).
    duplicate_db: Ledger = make_db("duplicate")
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
        first = handle_message(base_msg, edit_cfg, duplicate_db, "unused", False)
        # Sub-part quote or full phone quote in duplicate message
        duplicate_msg = {
            "chat": {"id": -1004417247378, "type": "supergroup"},
            "from": {"id": 8777968077, "first_name": "owner"},
            "message_id": 9102, "date": int(time.time()), "text": "20K ✅",
            "quote": {"text": "09675362816"},
            "reply_to_message": {"message_id": 7101, "text": "09675362816"},
        }
        second = handle_message(duplicate_msg, edit_cfg, duplicate_db, "unused", False)
    finally:
        tally_main.send = real_send

    dup_rows = duplicate_db.rows_for_day(duplicate_db.day_keys()[0])
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
