"""Telegram command dispatchers, callback handlers, and UI renderers."""

from __future__ import annotations

import re
import threading
from datetime import datetime

from src.core.config import TZ
from src.core.ledger import (
    LEDGER_LOCK,
    control_state,
    save_control,
    save_ledger,
    summarize,
    today_key,
)
from src.parser.amount_parser import fmt, label, normalize_search
from src.telegram.client import (
    answer_callback,
    chat_action,
    edit_list_message,
    send,
    verify_day,
)

LIST_PAGE_SIZE = 20

HELP = (
    "💰 <b>Tally bot</b>\n"
    "message ထဲ 10K / 25K / 5,000 ရေးလိုက်တာတွေကို နေ့စဉ် မှတ်တယ်။\n"
    "Edit လုပ်တာ၊ ဖျက်လိုက်တာ အလိုအလျောက် ပြန်ချိန်တယ်။\n\n"
    "/total — message အရေအတွက် + စုစုပေါင်း\n"
    "/details — ပိုက်ဆံအလိုက် အုပ်စုခွဲ (5K — 10 ခု ...)\n"
    "/list — message တစ်စောင်ချင်း (page ၄၀ စောင်စီ)\n"
    "/search 09672 — phone/reference number ရှာ\n"
    "/verify — ဖျက်လိုက်တာတွေ အကုန်စစ် (ကြာနိုင်)\n"
    "/dayclose — ဒီနေ့စာရင်းပိတ်\n"
    "/dayopen — ပိတ်ထားတဲ့နေ့ကို ပြန်ဖွင့်\n"
    "/maintenance — tally ခဏရပ်\n"
    "/active — tally ပြန်ဖွင့်\n"
    "/total 2026-08-21 — ရက်ရွေးကြည့်"
)


def _footer(s: dict, cfg: dict) -> str:
    """Warn only when verification is lagging, never for freshly-arrived rows."""
    if not s.get("stale"):
        return ""
    return f"\n<i>⚠️ {s['stale']} စောင် မစစ်ရသေး (Telegram မအဆင်ပြေ)</i>"


def render_total(s: dict, cfg: dict) -> str:
    cur = cfg["currency_suffix"]
    if not s["messages"]:
        return f"📊 {s['day']} — ဒီနေ့ ပိုက်ဆံ message မရှိသေးဘူး။"
    return (
        f"📊 <b>{s['day']}</b>\n"
        f"Message: <b>{s['messages']}</b> စောင်\n"
        f"စုစုပေါင်း: <b>{fmt(s['total'], cur)}</b>" + _footer(s, cfg)
    )


def render_details(s: dict, cfg: dict) -> str:
    """Grouped breakdown: one line per denomination, not per message."""
    cur = cfg["currency_suffix"]
    if not s["messages"]:
        return f"📋 {s['day']} — ဘာမှ မရှိသေးဘူး။"
    lines = [f"📋 <b>{s['day']} အသေးစိတ်</b>"]
    for amount in sorted(s["buckets"], reverse=True):
        count = s["buckets"][amount]
        lines.append(f"{label(amount)} — <b>{count}</b> ခု = {fmt(amount * count, cur)}")
    extra = f" • ပိုက်ဆံ <b>{s['items']}</b> ခု" if s["items"] != s["messages"] else ""
    lines.append(
        f"— — —\nMessage <b>{s['messages']}</b> စောင်{extra}\n"
        f"စုစုပေါင်း <b>{fmt(s['total'], cur)}</b>" + _footer(s, cfg)
    )
    return "\n".join(lines)


def list_page_count(s: dict, page_size: int = LIST_PAGE_SIZE) -> int:
    return max(1, (len(s["rows"]) + page_size - 1) // page_size)


def list_keyboard(s: dict, page: int, page_size: int = LIST_PAGE_SIZE) -> dict:
    total_pages = list_page_count(s, page_size)
    buttons = []
    if page > 1:
        buttons.append({"text": "‹ အဟောင်း", "callback_data": f"tally:list:{page - 1}"})
    buttons.append({"text": f"{page}/{total_pages}", "callback_data": "tally:noop"})
    if page < total_pages:
        buttons.append({"text": "အသစ် ›", "callback_data": f"tally:list:{page + 1}"})
    return {"inline_keyboard": [buttons]}


def render_list(s: dict, cfg: dict, page: int = 1, page_size: int = LIST_PAGE_SIZE) -> str:
    """Raw per-message listing, newest first, 20 rows per page."""
    cur = cfg["currency_suffix"]
    rows = s["rows"]
    if not rows:
        return f"📝 {s['day']} — ဘာမှ မရှိသေးဘူး။"
    page = max(1, int(page))
    total_pages = list_page_count(s, page_size)
    if page > total_pages:
        return f"📝 {s['day']} — page {page} မရှိဘူး။ (စုစုပေါင်း {total_pages} page)"
    end = len(rows) - (page - 1) * page_size
    start = max(0, end - page_size)
    shown = rows[start:end]
    lines = [f"📝 <b>{s['day']} message အလိုက်</b>"]
    lines.append(f"<i>Page {page}/{total_pages} • အသစ်ဆုံးကနေ စပြ</i>")
    for i, r in enumerate(shown, start=start + 1):
        clock = datetime.fromtimestamp(r["ts"], TZ).strftime("%H:%M")
        reference = r.get("reference_number")
        subject = f" {reference} —" if reference else " —"
        parts = " + ".join(label(a) for a in r["amounts"])
        detail = f" ({parts})" if len(r["amounts"]) > 1 else ""
        lines.append(f"{i}. {clock}{subject} <b>{fmt(r['total'], cur)}</b>{detail}")
    lines.append(f"— — —\nစုစုပေါင်း <b>{fmt(s['total'], cur)}</b>")
    return "\n".join(lines)


def render_search(ledger: dict, query: str, cfg: dict, day: str | None = None) -> str:
    """Find reference numbers by partial match across the local ledger."""
    needle = normalize_search(query)
    if len(needle) < 5 or len(needle) > 10:
        return "🔎 Search က 5 လုံးကနေ 10 လုံးအထိ ထည့်ပါ။ Space ပါလည်း ရတယ်။"
    cur = cfg["currency_suffix"]
    hits: list[tuple[str, dict]] = []
    for d, rows in ledger.items():
        if day and d != day:
            continue
        for row in rows:
            ref = normalize_search(row.get("reference_number", ""))
            if ref and needle in ref:
                hits.append((d, row))
    if not hits:
        scope = day or "သိမ်းထားတဲ့ ledger"
        return f"🔎 <b>{needle}</b> — {scope} ထဲမှာ မတွေ့ဘူး။"
    hits.sort(key=lambda item: item[1].get("ts", 0), reverse=True)
    lines = [f"🔎 <b>{needle}</b> — {len(hits)} ခုတွေ့တယ်"]
    for d, row in hits[:40]:
        clock = datetime.fromtimestamp(row["ts"], TZ).strftime("%m-%d %H:%M")
        ref = row.get("reference_number") or "?"
        lines.append(f"{clock} {ref} — <b>{fmt(row['total'], cur)}</b>")
    if len(hits) > 40:
        lines.append(f"<i>နောက်ဆုံး 40 ခုသာ ပြထား ({len(hits)} ခုလုံး)</i>")
    return "\n".join(lines)


def handle_command(cmd: str, arg: str, msg: dict, cfg: dict, ledger: dict, token: str) -> None:
    chat_id = msg.get("chat", {}).get("id")
    thread_id = msg.get("message_thread_id")
    reply_to = msg.get("message_id")
    day = arg if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg) else today_key()
    control = control_state(cfg)
    owners = cfg["owner_ids"]

    # Control commands are owner-only and persist across service restarts.
    if cmd in ("/dayclose", "/dayopen", "/maintenance", "/active"):
        sender_id = (msg.get("from") or {}).get("id")
        if owners and sender_id not in owners:
            return
        if cmd == "/dayclose":
            if day not in control["closed_days"]:
                control["closed_days"].append(day)
                save_control(control)
            chat_action(token, chat_id)
            send(
                token,
                chat_id,
                f"🔒 Tally for {day} is now closed.\n"
                "New tally messages for this day will be ignored.",
                reply_to,
                thread_id,
            )
        elif cmd == "/dayopen":
            control["closed_days"] = [d for d in control["closed_days"] if d != day]
            save_control(control)
            chat_action(token, chat_id)
            send(
                token,
                chat_id,
                f"🔓 Tally for {day} is open again.\nNew tally messages will be counted.",
                reply_to,
                thread_id,
            )
        elif cmd == "/maintenance":
            control["maintenance"] = True
            save_control(control)
            chat_action(token, chat_id)
            send(
                token,
                chat_id,
                "🔧 Tally Bot is currently in Maintenance Mode.\n"
                "Please do not send tally messages until further notice.\n"
                "The ledger is being audited and corrected.",
                reply_to,
                thread_id,
            )
        else:  # /active
            control["maintenance"] = False
            save_control(control)
            chat_action(token, chat_id)
            send(
                token,
                chat_id,
                "✅ Tally Bot is Active again.\n"
                "The ledger audit and correction are complete.\n"
                "You may resume sending tally messages.",
                reply_to,
                thread_id,
            )
        return

    if cmd in ("/help", "/start"):
        chat_action(token, chat_id)
        send(token, chat_id, HELP, reply_to, thread_id)
        return

    if cmd == "/verify":
        if day in control.get("closed_days", []):
            chat_action(token, chat_id)
            send(
                token,
                chat_id,
                f"🔒 Tally for {day} is closed. Use /dayopen before making changes.",
                reply_to,
                thread_id,
            )
            return
        chat_action(token, chat_id)
        pending = len(ledger.get(day) or [])
        send(
            token,
            chat_id,
            f"🔍 <b>{day}</b> — {pending} စောင်ထဲက ပြန်စစ်ရမယ့် စောင်တွေကို စစ်နေတယ်၊ "
            "ပြီးရင် ပြန်ပြောမယ်။",
            reply_to,
            thread_id,
        )

        def worker() -> None:
            removed, checked = verify_day(token, ledger, day, cfg, budget_seconds=None)
            with LEDGER_LOCK:
                save_ledger(ledger)
            done = summarize(ledger, day, cfg)
            send(
                token,
                chat_id,
                f"✅ <b>{day}</b> စစ်ပြီး — ပြန်စစ်ရမယ့် {checked} စောင်ကို စစ်၊ "
                f"ဖျက်ထားတာ <b>{removed}</b> စောင် ဖယ်ပြီး။\n"
                f"စုစုပေါင်း <b>{fmt(done['total'], cfg['currency_suffix'])}</b>",
                reply_to,
                thread_id,
            )

        threading.Thread(target=worker, daemon=True).start()
        return

    removed = 0
    chat_action(token, chat_id)
    if cfg["verify_on_read"]:
        removed, _ = verify_day(token, ledger, day, cfg, cfg["verify_budget_seconds"])
        if removed:
            with LEDGER_LOCK:
                save_ledger(ledger)
    s = summarize(ledger, day, cfg)
    note = f"\n<i>(ဖျက်ထားတာ {removed} စောင် ဖယ်ပြီး)</i>" if removed else ""

    if cmd == "/total":
        send(token, chat_id, render_total(s, cfg) + note, reply_to, thread_id)
    elif cmd == "/details":
        send(token, chat_id, render_details(s, cfg) + note, reply_to, thread_id)
    elif cmd == "/list":
        page = int(arg) if arg.isdigit() else 1
        send(
            token,
            chat_id,
            render_list(s, cfg, page=page) + note,
            reply_to,
            thread_id,
            list_keyboard(s, page),
        )
    elif cmd == "/search":
        send(token, chat_id, render_search(ledger, arg, cfg), reply_to, thread_id)


def handle_callback(query: dict, cfg: dict, ledger: dict, token: str) -> None:
    """Handle inline list navigation without sending a new Telegram message."""
    answer_callback(token, query.get("id", ""))
    data = query.get("data", "")
    if data == "tally:noop":
        return
    match = re.fullmatch(r"tally:list:(\d+)", data)
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if not match or not chat_id or not message_id:
        return
    page = max(1, int(match.group(1)))
    day = today_key()
    summary = summarize(ledger, day, cfg)
    text = render_list(summary, cfg, page=page)
    edit_list_message(token, chat_id, message_id, text, list_keyboard(summary, page))
