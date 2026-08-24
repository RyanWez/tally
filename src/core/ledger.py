"""SQLite-backed ledger persistence, CRUD operations, and summarize helpers.

Storage layout (single file, WAL mode):
    entries table : one row per tallied message; PK (chat_id, message_id)
    meta   table  : poll offset + migration flag
    control table : maintenance flag + closed_days (JSON encoded)

Threading model: one shared connection, every access serialized through
LEDGER_LOCK (RLock). Mutations commit immediately - there is no deferred
save step anymore.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.core.config import (
    CONTROL_PATH,
    KEEP_DAYS,
    LEDGER_PATH,
    OFFSET_PATH,
    TZ,
    load_json,
)
from src.parser.amount_parser import is_reference_match, normalize_search

LEDGER_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    chat_id             INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    day                 TEXT    NOT NULL,
    ts                  INTEGER NOT NULL,
    sender_id           INTEGER,
    sender_name         TEXT,
    username            TEXT,
    amounts             TEXT    NOT NULL,
    total               INTEGER NOT NULL,
    edited              INTEGER NOT NULL DEFAULT 0,
    vat                 INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id INTEGER,
    reference_number    TEXT,
    original_text       TEXT,
    reply_text          TEXT,
    quote_text          TEXT,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_day_ts ON entries(day, ts);
CREATE INDEX IF NOT EXISTS idx_entries_ref    ON entries(reference_number);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Column order used for every SELECT; _row_to_dict depends on it.
_ROW_COLUMNS = (
    "chat_id",
    "message_id",
    "ts",
    "sender_id",
    "sender_name",
    "username",
    "amounts",
    "total",
    "edited",
    "vat",
    "reply_to_message_id",
    "reference_number",
    "original_text",
    "reply_text",
    "quote_text",
)

_INSERT_COLUMNS = (
    "chat_id",
    "message_id",
    "day",
    *_ROW_COLUMNS[2:],
)


def _row_to_dict(row: tuple) -> dict:
    d = dict(zip(_ROW_COLUMNS, row))
    d["amounts"] = json.loads(d["amounts"])
    d["edited"] = bool(d["edited"])
    return d


def today_key(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).strftime("%Y-%m-%d")


def control_state(cfg: dict) -> dict:
    return cfg.setdefault("_control", {"maintenance": False, "closed_days": []})


def tally_paused(cfg: dict, day: str) -> bool:
    control = control_state(cfg)
    return bool(control.get("maintenance") or day in control.get("closed_days", []))


def sender_matches(row: dict, query: str) -> bool:
    """Does a ledger row belong to the queried person?

    Accepts @username, stored display name (exact first, substring second),
    or numeric Telegram ID.
    """
    q = (query or "").strip().lstrip("@").lower()
    if not q:
        return True
    if q.isdigit():
        return str(row.get("sender_id") or "") == q
    uname = (row.get("username") or "").lower()
    name = (row.get("sender_name") or "").lower()
    return q == uname or q == name or q in name or (bool(uname) and q in uname)


class Ledger:
    """Single-file SQLite ledger. All access is serialized via LEDGER_LOCK."""

    def __init__(self, path: str | Path, import_legacy: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        with LEDGER_LOCK:
            self.conn.executescript(_SCHEMA)
            self._ensure_columns()
            if import_legacy:
                self._import_legacy()

    def _ensure_columns(self) -> None:
        """Add columns introduced after the original schema (idempotent)."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(entries)").fetchall()}
        if "username" not in cols:
            self.conn.execute("ALTER TABLE entries ADD COLUMN username TEXT")

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        with LEDGER_LOCK:
            self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- writes
    @staticmethod
    def _entry_params(day: str, entry: dict) -> tuple:
        return (
            entry.get("chat_id"),
            entry.get("message_id"),
            day,
            int(entry.get("ts") or 0),
            entry.get("sender_id"),
            entry.get("sender_name"),
            entry.get("username"),
            json.dumps(entry.get("amounts") or []),
            int(entry.get("total") or 0),
            int(bool(entry.get("edited"))),
            int(entry.get("vat") or 0),
            entry.get("reply_to_message_id"),
            entry.get("reference_number"),
            entry.get("original_text"),
            entry.get("reply_text"),
            entry.get("quote_text"),
        )

    def record(self, day: str, entry: dict) -> None:
        """Insert a row, replacing any existing row with the same chat+message."""
        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _INSERT_COLUMNS[2:])
        with LEDGER_LOCK:
            self.conn.execute(
                f"INSERT INTO entries ({', '.join(_INSERT_COLUMNS)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT(chat_id, message_id) DO UPDATE SET {updates}",
                self._entry_params(day, entry),
            )

    def remove_message(self, chat_id, message_id) -> bool:
        """Remove every row for an edited/deleted message, across all days."""
        if message_id is None:
            return False
        with LEDGER_LOCK:
            cur = self.conn.execute(
                "DELETE FROM entries WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            return bool(cur.rowcount)

    def apply_verify_results(
        self,
        alive_ids: list[tuple],
        dead_ids: list[tuple],
        stamp: int,
    ) -> int:
        """Stamp verified-at on live rows and delete probed-dead ones atomically."""
        removed = 0
        with LEDGER_LOCK:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for chat_id, message_id in alive_ids:
                    self.conn.execute(
                        "UPDATE entries SET vat = ? "
                        "WHERE chat_id = ? AND message_id = ?",
                        (stamp, chat_id, message_id),
                    )
                for chat_id, message_id in dead_ids:
                    cur = self.conn.execute(
                        "DELETE FROM entries WHERE chat_id = ? AND message_id = ?",
                        (chat_id, message_id),
                    )
                    removed += max(0, cur.rowcount)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return removed

    def prune(self) -> int:
        cutoff = (datetime.now(TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        with LEDGER_LOCK:
            cur = self.conn.execute("DELETE FROM entries WHERE day < ?", (cutoff,))
            return max(0, cur.rowcount)

    # ----------------------------------------------------------------- reads
    def rows_for_day(self, day: str) -> list[dict]:
        cols = ", ".join(_ROW_COLUMNS)
        with LEDGER_LOCK:
            rows = self.conn.execute(
                f"SELECT {cols} FROM entries WHERE day = ? "
                "ORDER BY ts ASC, message_id ASC",
                (day,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def count_rows(self, day: str) -> int:
        with LEDGER_LOCK:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM entries WHERE day = ?", (day,)
            )
            return int(cur.fetchone()[0])

    def day_keys(self) -> list[str]:
        """All days holding rows, newest first."""
        with LEDGER_LOCK:
            rows = self.conn.execute(
                "SELECT DISTINCT day FROM entries ORDER BY day DESC"
            ).fetchall()
        return [r[0] for r in rows]

    def latest_row(self, chat_id, day: str) -> dict | None:
        """Newest row for a chat on a day (tie-break: higher message_id)."""
        cols = ", ".join(_ROW_COLUMNS)
        with LEDGER_LOCK:
            row = self.conn.execute(
                f"SELECT {cols} FROM entries WHERE chat_id = ? AND day = ? "
                "ORDER BY ts DESC, message_id DESC LIMIT 1",
                (chat_id, day),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def rows_matching_reference(self, chat_id, needle: str, day: str | None = None) -> list[dict]:
        """Rows for one chat whose stored reference matches the digit needle.

        day=None scans every recorded day (used to hint where a reference lives).
        """
        hits: list[dict] = []
        for d in [day] if day else self.day_keys():
            for row in self.rows_for_day(d):
                if row.get("chat_id") != chat_id:
                    continue
                if is_reference_match(needle, row.get("reference_number")):
                    hits.append(row)
        return hits

    def find_reference(
        self,
        day: str,
        reference: str | None,
        exclude_chat_id=None,
        exclude_message_id=None,
    ) -> dict | None:
        """Find an already-recorded reference for the same local day."""
        if not reference:
            return None
        for row in self.rows_for_day(day):
            if row["chat_id"] == exclude_chat_id and row["message_id"] == exclude_message_id:
                continue
            if is_reference_match(reference, row.get("reference_number")):
                return row
        return None

    def search_reference(self, needle: str) -> list[tuple[str, dict]]:
        """Rows whose normalized reference contains the digit needle, newest first.

        Scans in Python (not SQL LIKE) so legacy references containing spaces or
        dashes still match after normalization, exactly like the old renderer.
        """
        hits: list[tuple[str, dict]] = []
        for day in self.day_keys():
            for row in self.rows_for_day(day):
                ref = normalize_search(row.get("reference_number", ""))
                if ref and needle in ref:
                    hits.append((day, row))
        hits.sort(key=lambda item: item[1].get("ts", 0), reverse=True)
        return hits

    def rows_needing_check(
        self,
        day: str,
        now: float,
        grace_seconds: float,
        fresh_window_seconds: float,
        recheck_fresh_seconds: float,
        recheck_old_seconds: float,
    ) -> list[dict]:
        """Rows past their grace period whose verification stamp went stale.

        Mirrors the previous _needs_check/_recheck_interval pair:
        never-verified rows are always due; otherwise due when
        now - vat >= recheck_fresh (young row) / recheck_old (aged row).
        """
        cols = ", ".join(_ROW_COLUMNS)
        with LEDGER_LOCK:
            rows = self.conn.execute(
                f"SELECT {cols} FROM entries "
                "WHERE day = ? "
                "  AND (? - ts) >= ? "
                "  AND (? - COALESCE(vat, 0)) >= "
                "      CASE WHEN (? - ts) < ? THEN ? ELSE ? END "
                "ORDER BY vat ASC, ts ASC",
                (
                    day,
                    now,
                    grace_seconds,
                    now,
                    now,
                    fresh_window_seconds,
                    recheck_fresh_seconds,
                    recheck_old_seconds,
                ),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def summarize(self, day: str, cfg: dict, sender: str | None = None) -> dict:
        rows = self.rows_for_day(day)
        if cfg.get("count_only_owner") and cfg.get("owner_ids"):
            rows = [r for r in rows if r.get("sender_id") in cfg["owner_ids"]]
        if sender:
            rows = [r for r in rows if sender_matches(r, sender)]
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

    # ------------------------------------------------------- offset & control
    def get_offset(self) -> int:
        with LEDGER_LOCK:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'offset'"
            ).fetchone()
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    def set_offset(self, offset: int) -> None:
        with LEDGER_LOCK:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('offset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(int(offset)),),
            )

    def load_control(self) -> dict:
        control: dict = {}
        with LEDGER_LOCK:
            rows = self.conn.execute("SELECT key, value FROM control").fetchall()
        for key, value in rows:
            try:
                control[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
        control.setdefault("maintenance", False)
        control.setdefault("closed_days", [])
        return control

    def save_control(self, control: dict) -> None:
        pairs = [
            ("maintenance", json.dumps(bool(control.get("maintenance", False)))),
            ("closed_days", json.dumps(list(control.get("closed_days", [])))),
        ]
        with LEDGER_LOCK:
            self.conn.executemany(
                "INSERT INTO control (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                pairs,
            )

    # -------------------------------------------------------------- migration
    def _meta_get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _import_legacy(self) -> None:
        """One-time import of the old JSON files into SQLite.

        Non-destructive: the original files are left untouched so they keep
        working as a backup. Guarded by a meta flag, so it runs only once.
        """
        if self._meta_get("legacy_imported"):
            return
        try:
            imported_rows = 0
            legacy_ledger: dict = load_json(LEDGER_PATH, {})
            params = []
            for day, entries in (legacy_ledger or {}).items():
                for entry in entries or []:
                    params.append(self._entry_params(str(day), entry))
            if params:
                placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
                updates = ", ".join(f"{c} = excluded.{c}" for c in _INSERT_COLUMNS[2:])
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.executemany(
                    f"INSERT INTO entries ({', '.join(_INSERT_COLUMNS)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT(chat_id, message_id) DO UPDATE SET {updates}",
                    params,
                )
                self.conn.execute("COMMIT")
                imported_rows = len(params)

            offset = (load_json(OFFSET_PATH, {}) or {}).get("offset")
            if isinstance(offset, int) and offset > 0:
                self.set_offset(offset)

            legacy_control = load_json(CONTROL_PATH, {}) or {}
            if legacy_control:
                self.save_control(legacy_control)

            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('legacy_imported', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            if imported_rows or offset or legacy_control:
                print(
                    f"[migrate] imported {imported_rows} ledger row(s), "
                    f"offset={offset if isinstance(offset, int) else 'n/a'}, "
                    f"control={'yes' if legacy_control else 'no'} from legacy JSON",
                    flush=True,
                )
        except Exception as exc:  # startup must survive a bad legacy file
            print(f"[migrate] legacy import skipped: {type(exc).__name__}: {exc}", flush=True)
