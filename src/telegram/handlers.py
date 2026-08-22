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
    "💰 <b>Tally Bot</b>\n"
    "Daily expense & amount tallying from chat messages (e.g. 10K / 25K / 5,000).\n"
    "Automatically handles edits and message deletions.\n\n"
    "/total — Message count + grand total\n"
    "/details — Grouped breakdown by denomination (5K — 10 items ...)\n"
    "/list — Message log with pagination (20 per page)\n"
    "/search 09672 — Search by phone/reference number\n"
    "/verify — Probe & clean up deleted messages\n"
    "/dayclose — Close today's ledger (Owner only)\n"
    "/dayopen — Reopen a closed day's ledger (Owner only)\n"
    "/maintenance — Temporarily pause tallying (Owner only)\n"
    "/active — Resume tallying (Owner only)\n"
    "/total 2026-08-21 — View total for a specific date"
)


def _footer(s: dict, cfg: dict) -> str:
    """Warn only when verification is lagging, never for freshly-arrived rows."""
    if not s.get("stale"):
        return ""
    count = s["stale"]
    return f"\n<i>⚠️ {count} message{'s' if count != 1 else ''} unverified (Telegram connection issue)</i>"


def render_total(s: dict, cfg: dict) -> str:
    cur = cfg["currency_suffix"]
    if not s["messages"]:
        return f"📊 {s['day']} — No tally messages recorded yet."
    return (
        f"📊 <b>{s['day']}</b>\n"
        f"Messages: <b>{s['messages']}</b>\n"
        f"Total: <b>{fmt(s['total'], cur)}</b>" + _footer(s, cfg)
    )


def render_details(s: dict, cfg: dict) -> str:
    """Grouped breakdown: one line per denomination, not per message."""
    cur = cfg["currency_suffix"]
    if not s["messages"]:
        return f"📋 {s['day']} — No records found."
    lines = [f"📋 <b>{s['day']} Details</b>"]
    for amount in sorted(s["buckets"], reverse=True):
        count = s["buckets"][amount]
        lines.append(f"{label(amount)} — <b>{count}</b> item{'s' if count != 1 else ''} = {fmt(amount * count, cur)}")
    extra = f" • <b>{s['items']}</b> item{'s' if s['items'] != 1 else ''}" if s["items"] != s["messages"] else ""
    lines.append(
        f"— — —\nMessages: <b>{s['messages']}</b>{extra}\n"
        f"Total: <b>{fmt(s['total'], cur)}</b>" + _footer(s, cfg)
    )
    return "\n".join(lines)


def list_page_count(s: dict, page_size: int = LIST_PAGE_SIZE) -> int:
    return max(1, (len(s["rows"]) + page_size - 1) // page_size)


def list_keyboard(s: dict, page: int, page_size: int = LIST_PAGE_SIZE) -> dict:
    total_pages = list_page_count(s, page_size)
    # Callbacks carry the rendered day so paging a past-day view stays on it.
    day = s["day"]
    buttons = []
    if page > 1:
        buttons.append({"text": "‹ Prev", "callback_data": f"tally:list:{day}:{page - 1}"})
    buttons.append({"text": f"{page}/{total_pages}", "callback_data": "tally:noop"})
    if page < total_pages:
        buttons.append({"text": "Next ›", "callback_data": f"tally:list:{day}:{page + 1}"})
    return {"inline_keyboard": [buttons]}


def render_list(s: dict, cfg: dict, page: int = 1, page_size: int = LIST_PAGE_SIZE) -> str:
    """Raw per-message listing, newest first, 20 rows per page."""
    cur = cfg["currency_suffix"]
    rows = s["rows"]
    if not rows:
        return f"📝 {s['day']} — No records found."
    page = max(1, int(page))
    total_pages = list_page_count(s, page_size)
    if page > total_pages:
        return f"📝 {s['day']} — Page {page} not found. (Total {total_pages} pages)"
    end = len(rows) - (page - 1) * page_size
    start = max(0, end - page_size)
    shown = rows[start:end]
    lines = [f"📝 <b>{s['day']} Message Log</b>"]
    lines.append(f"<i>Page {page}/{total_pages} • Newest first</i>")
    for i, r in enumerate(shown, start=start + 1):
        clock = datetime.fromtimestamp(r["ts"], TZ).strftime("%H:%M")
        reference = r.get("reference_number")
        subject = f" {reference} —" if reference else " —"
        parts = " + ".join(label(a) for a in r["amounts"])
        detail = f" ({parts})" if len(r["amounts"]) > 1 else ""
        lines.append(f"{i}. {clock}{subject} <b>{fmt(r['total'], cur)}</b>{detail}")
    lines.append(f"— — —\nTotal: <b>{fmt(s['total'], cur)}</b>")
    return "\n".join(lines)


def render_search(ledger: dict, query: str, cfg: dict, day: str | None = None) -> str:
    """Find reference numbers by partial match across the local ledger."""
    needle = normalize_search(query)
    if len(needle) < 5 or len(needle) > 11:
        return "🔎 Search query must be between 5 and 11 digits (spaces allowed)."
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
        scope = day or "ledger"
        return f"🔎 <b>{needle}</b> — Not found in {scope}."
    hits.sort(key=lambda item: item[1].get("ts", 0), reverse=True)
    count = len(hits)
    lines = [f"🔎 <b>{needle}</b> — Found {count} match{'es' if count != 1 else ''}"]
    for d, row in hits[:40]:
        clock = datetime.fromtimestamp(row["ts"], TZ).strftime("%m-%d %H:%M")
        ref = row.get("reference_number") or "?"
        lines.append(f"{clock} {ref} — <b>{fmt(row['total'], cur)}</b>")
    if len(hits) > 40:
        lines.append(f"<i>Showing latest 40 of {len(hits)} matches</i>")
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
            f"🔍 <b>{day}</b> — Verifying {pending} message(s), will report when finished.",
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
                f"✅ <b>{day}</b> Verification complete — Checked {checked} message(s), "
                f"removed <b>{removed}</b> deleted message(s).\n"
                f"Total: <b>{fmt(done['total'], cfg['currency_suffix'])}</b>",
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
    note = f"\n<i>({removed} deleted message{'s' if removed != 1 else ''} removed)</i>" if removed else ""

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
    # New format: tally:list:YYYY-MM-DD:N. Keyboards rendered before the day
    # was embedded (plain tally:list:N) fall back to today.
    match = re.fullmatch(r"tally:list:(\d{4}-\d{2}-\d{2}):(\d+)", data)
    if match:
        day, page = match.group(1), max(1, int(match.group(2)))
    else:
        legacy = re.fullmatch(r"tally:list:(\d+)", data)
        if not legacy:
            return
        day, page = today_key(), max(1, int(legacy.group(1)))
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if not chat_id or not message_id:
        return
    summary = summarize(ledger, day, cfg)
    text = render_list(summary, cfg, page=page)
    edit_list_message(token, chat_id, message_id, text, list_keyboard(summary, page))
