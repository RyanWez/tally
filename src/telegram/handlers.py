"""Telegram command dispatchers, callback handlers, and UI renderers."""

from __future__ import annotations

import html
import re
import threading
from datetime import datetime

from src.core.config import TZ
from src.core.ledger import control_state, sender_matches, today_key
from src.parser.amount_parser import fmt, label, normalize_search
from src.telegram.client import (
    answer_callback,
    chat_action,
    edit_list_message,
    send,
    verify_day,
)

LIST_PAGE_SIZE = 20
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

HELP = (
    "💰 <b>Tally Bot</b>\n"
    "Daily expense & amount tallying from chat messages (e.g. 10K / 25K / 5,000).\n"
    "Automatically handles edits and message deletions.\n\n"
    "/total — Message count + grand total\n"
    "/total @Alice — One person's count & total\n"
    "/details — Grouped breakdown by denomination (5K — 10 items ...)\n"
    "/details @Alice — Breakdown for one person\n"
    "/list — Message log with pagination (20 per page)\n"
    "/search 09672 — Search by phone/reference number\n"
    "/verify — Probe & clean up deleted messages\n"
    "/undo [date] — Remove the most recent tally of the day (Owner only)\n"
    "/delete 09672... — Delete a wrong entry by reference (Owner only)\n"
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
        who = f" for <i>{html.escape(str(s['sender_label']))}</i>" if s.get("sender_label") else ""
        return f"📊 {s['day']} —{who} No tally messages recorded yet."
    who = f" — <i>{html.escape(str(s['sender_label']))}</i>" if s.get("sender_label") else ""
    return (
        f"📊 <b>{s['day']}</b>{who}\n"
        f"Messages: <b>{s['messages']}</b>\n"
        f"Total: <b>{fmt(s['total'], cur)}</b>" + _footer(s, cfg)
    )


def render_details(s: dict, cfg: dict) -> str:
    """Grouped breakdown: one line per denomination, not per message."""
    cur = cfg["currency_suffix"]
    who = f" — <i>{html.escape(str(s['sender_label']))}</i>" if s.get("sender_label") else ""
    if not s["messages"]:
        return f"📋 {s['day']}{who} — No records found."
    lines = [f"📋 <b>{s['day']} Details</b>{who}"]
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


def render_search(db, query: str, cfg: dict, day: str | None = None) -> str:
    """Find reference numbers by partial match across the local ledger."""
    needle = normalize_search(query)
    if len(needle) < 5 or len(needle) > 11:
        return "🔎 Search query must be between 5 and 11 digits (spaces allowed)."
    cur = cfg["currency_suffix"]
    hits: list[tuple[str, dict]] = [
        (d, row)
        for d, row in db.search_reference(needle)
        if not day or d == day
    ]
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


def parse_query(arg: str) -> tuple[str, str]:
    """Split a command argument into (sender_query, day).

    Accepts any order: "/details @Alice 2026-08-21" or "/total 2026-08-21".
    """
    sender = ""
    day = ""
    for tok in arg.split():
        if _DATE_RE.fullmatch(tok):
            day = tok
        elif not sender:
            sender = tok.lstrip("@")
    return sender, day or today_key()


def resolve_sender_label(db, day: str, query: str) -> str:
    """Best display name for a sender query, as recorded in the ledger."""
    for row in db.rows_for_day(day):
        if sender_matches(row, query):
            if row.get("username"):
                return f"@{row['username']}"
            return row.get("sender_name") or f"@{query}"
    return f"@{query}"


def handle_command(cmd: str, arg: str, msg: dict, cfg: dict, db, token: str) -> None:
    chat_id = msg.get("chat", {}).get("id")
    thread_id = msg.get("message_thread_id")
    reply_to = msg.get("message_id")
    day = arg if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg) else today_key()
    control = control_state(cfg)
    owners = cfg["owner_ids"]

    # Control commands are owner-only and persist across service restarts.
    if cmd in ("/dayclose", "/dayopen", "/maintenance", "/active", "/undo", "/delete"):
        sender_id = (msg.get("from") or {}).get("id")
        if owners and sender_id not in owners:
            return
        cur = cfg.get("currency_suffix", "")

        def closed_guard(target_day: str) -> bool:
            if target_day in control.get("closed_days", []):
                chat_action(token, chat_id)
                send(
                    token,
                    chat_id,
                    f"🔒 Tally for {target_day} is closed. Use /dayopen before making changes.",
                    reply_to,
                    thread_id,
                )
                return True
            return False

        if cmd == "/undo":
            if closed_guard(day):
                return
            row = db.latest_row(chat_id, day)
            if not row:
                send(
                    token,
                    chat_id,
                    f"↩️ {day} — Nothing to undo.",
                    reply_to,
                    thread_id,
                )
                return
            db.remove_message(row["chat_id"], row["message_id"])
            clock = datetime.fromtimestamp(row["ts"], TZ).strftime("%H:%M")
            ref = f" {row['reference_number']}" if row.get("reference_number") else ""
            who = f"@{row['username']}" if row.get("username") else (row.get("sender_name") or "?")
            after = db.summarize(day, cfg)
            chat_action(token, chat_id)
            send(
                token,
                chat_id,
                f"↩️ Undone: {clock}{ref} — <b>{fmt(row['total'], cur)}</b> by {html.escape(str(who))}\n"
                f"{day} total now: <b>{fmt(after['total'], cur)}</b> ({after['messages']} message{'s' if after['messages'] != 1 else ''})",
                reply_to,
                thread_id,
            )
            print(f"[undo] day={day} msg={row.get('message_id')} removed={row['total']}", flush=True)
            return

        if cmd == "/delete":
            del_ref, del_day = "", day
            for tok in arg.split():
                if _DATE_RE.fullmatch(tok):
                    del_day = tok
                elif not del_ref:
                    del_ref = tok
            needle = normalize_search(del_ref)
            if len(needle) < 5:
                send(
                    token,
                    chat_id,
                    "Usage: <b>/delete &lt;phone/reference&gt;</b> [YYYY-MM-DD]\n"
                    "Example: /delete 09675362816",
                    reply_to,
                    thread_id,
                )
                return
            if closed_guard(del_day):
                return
            hits = db.rows_matching_reference(chat_id, needle, del_day)
            if len(hits) > 1:  # partial needle matched several rows; ask to be specific
                lines = [f"🔎 {len(hits)} entries match <b>{needle}</b> — use a fuller number:"]
                for r in sorted(hits, key=lambda x: x.get("ts", 0), reverse=True):
                    clock = datetime.fromtimestamp(r["ts"], TZ).strftime("%H:%M")
                    lines.append(f"{clock} {r.get('reference_number') or '?'} — <b>{fmt(r['total'], cur)}</b>")
                send(token, chat_id, "\n".join(lines), reply_to, thread_id)
                return
            if not hits:
                other_days = sorted({
                    d
                    for d in db.day_keys()
                    if d != del_day
                    for r in db.rows_matching_reference(chat_id, needle, d)
                })
                hint = (
                    f"\nFound on: {', '.join(other_days)}\nUse /delete {del_ref} <date>."
                    if other_days
                    else ""
                )
                send(
                    token,
                    chat_id,
                    f"🔎 <b>{needle}</b> — Not found on {del_day}.{hint}",
                    reply_to,
                    thread_id,
                )
                return
            row = hits[0]
            db.remove_message(row["chat_id"], row["message_id"])
            clock = datetime.fromtimestamp(row["ts"], TZ).strftime("%H:%M")
            who = f"@{row['username']}" if row.get("username") else (row.get("sender_name") or "?")
            after = db.summarize(del_day, cfg)
            chat_action(token, chat_id)
            send(
                token,
                chat_id,
                f"🗑 Deleted: {clock} {row.get('reference_number') or '?'} — "
                f"<b>{fmt(row['total'], cur)}</b> (by {html.escape(str(who))})\n"
                f"{del_day} total now: <b>{fmt(after['total'], cur)}</b> ({after['messages']} message{'s' if after['messages'] != 1 else ''})",
                reply_to,
                thread_id,
            )
            print(
                f"[delete] day={del_day} msg={row.get('message_id')} ref={row.get('reference_number')} removed={row['total']}",
                flush=True,
            )
            return

        if cmd == "/dayclose":
            if day not in control["closed_days"]:
                control["closed_days"].append(day)
                db.save_control(control)
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
            db.save_control(control)
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
            db.save_control(control)
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
            db.save_control(control)
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
        pending = db.count_rows(day)
        send(
            token,
            chat_id,
            f"🔍 <b>{day}</b> — Verifying {pending} message(s), will report when finished.",
            reply_to,
            thread_id,
        )

        def worker() -> None:
            removed, checked = verify_day(token, db, day, cfg, budget_seconds=None)
            done = db.summarize(day, cfg)
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
        removed, _ = verify_day(token, db, day, cfg, cfg["verify_budget_seconds"])
    if cmd in ("/total", "/details"):
        who, view_day = parse_query(arg)
        s = db.summarize(view_day, cfg, sender=who or None)
        if who:
            s["sender_label"] = resolve_sender_label(db, view_day, who)
    else:
        s = db.summarize(day, cfg)
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
        send(token, chat_id, render_search(db, arg, cfg), reply_to, thread_id)


def handle_callback(query: dict, cfg: dict, db, token: str) -> None:
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
    summary = db.summarize(day, cfg)
    text = render_list(summary, cfg, page=page)
    edit_list_message(token, chat_id, message_id, text, list_keyboard(summary, page))
