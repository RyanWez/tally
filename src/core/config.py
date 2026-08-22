"""Configuration, paths, timezone, and environment loader."""

from __future__ import annotations

import json
import os
from datetime import timedelta, timezone
from pathlib import Path

# Project paths
BASE = Path(__file__).resolve().parent.parent.parent
STATE = BASE / "state"
CONFIG_PATH = BASE / "config.json"
LEDGER_PATH = STATE / "ledger.json"       # legacy JSON ledger (read during migration)
OFFSET_PATH = STATE / "offset.json"       # legacy offset file (read during migration)
CONTROL_PATH = STATE / "control.json"     # legacy control file (read during migration)
DB_PATH = STATE / "tally.db"              # SQLite database (single source of truth)
ENV_PATH = BASE / ".env"

KEEP_DAYS = 120


def _tz():
    """Asia/Yangon (or Asia/Rangoon), with a fixed +06:30 fallback when tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo

        try:
            return ZoneInfo("Asia/Yangon")
        except Exception:
            return ZoneInfo("Asia/Rangoon")
    except Exception:
        return timezone(timedelta(hours=6, minutes=30), "MMT")


TZ = _tz()


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Parse .env file into environment variables without external dependencies."""
    env_file = path or ENV_PATH
    loaded: dict[str, str] = {}
    if not env_file.exists():
        return loaded
    try:
        with env_file.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    loaded[k] = v
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass
    return loaded


def load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


def _parse_id_list(value: str | list | None) -> list[int]:
    if not value:
        return []
    if isinstance(value, list):
        return [int(x) for x in value if str(x).lstrip("-").isdigit()]
    if isinstance(value, str):
        items = [x.strip() for x in value.split(",") if x.strip()]
        return [int(x) for x in items if x.lstrip("-").isdigit()]
    return []


def _parse_denominations(value: str | list | None) -> list[int]:
    default = [5000, 10000, 15000, 20000, 25000]
    if not value:
        return default
    if isinstance(value, list):
        parsed = [int(x) for x in value if str(x).isdigit()]
        return sorted(list(set(parsed))) if parsed else default
    result: list[int] = []
    for item in str(value).split(","):
        item = item.strip().upper()
        if not item:
            continue
        if item.endswith("K"):
            try:
                result.append(int(float(item[:-1]) * 1000))
            except ValueError:
                pass
        elif item.isdigit():
            result.append(int(item))
    return sorted(list(set(result))) if result else default


def load_config(config_path: Path | None = None) -> dict:
    """Load configuration from .env, config.json, and environment variables."""
    load_dotenv()
    path = config_path or CONFIG_PATH
    cfg = load_json(path, {})

    # Bot token: Env var > config.json > token file
    token = (
        os.environ.get("TALLY_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or cfg.get("bot_token")
        or ""
    ).strip()
    token_file = cfg.get("bot_token_file") or os.environ.get("TALLY_BOT_TOKEN_FILE")
    if not token and token_file:
        p = Path(os.path.expanduser(token_file))
        if p.exists():
            token = p.read_text(encoding="utf-8").strip()
    cfg["bot_token"] = token

    # Chat and owner IDs: Env overrides config if set
    if "ALLOWED_CHAT_IDS" in os.environ:
        cfg["allowed_chat_ids"] = _parse_id_list(os.environ["ALLOWED_CHAT_IDS"])
    else:
        cfg.setdefault("allowed_chat_ids", [])

    if "OWNER_IDS" in os.environ:
        cfg["owner_ids"] = _parse_id_list(os.environ["OWNER_IDS"])
    else:
        cfg.setdefault("owner_ids", [])

    if "COUNT_ONLY_OWNER" in os.environ:
        cfg["count_only_owner"] = os.environ["COUNT_ONLY_OWNER"].lower() in ("1", "true", "yes")
    else:
        cfg.setdefault("count_only_owner", True)

    # Require Reply to tally amount messages (default True)
    if "REQUIRE_REPLY" in os.environ:
        cfg["require_reply"] = os.environ["REQUIRE_REPLY"].lower() in ("1", "true", "yes")
    else:
        cfg.setdefault("require_reply", True)

    # Denominations rules: [5000, 10000, 15000, 20000, 25000]
    if "ALLOWED_DENOMINATIONS" in os.environ:
        cfg["allowed_denominations"] = _parse_denominations(os.environ["ALLOWED_DENOMINATIONS"])
    else:
        cfg["allowed_denominations"] = _parse_denominations(cfg.get("allowed_denominations"))

    cfg.setdefault(
        "min_allowed_amount",
        int(os.environ.get("MIN_ALLOWED_AMOUNT", cfg.get("min_allowed_amount", 5000))),
    )
    cfg.setdefault(
        "max_allowed_amount",
        int(os.environ.get("MAX_ALLOWED_AMOUNT", cfg.get("max_allowed_amount", 25000))),
    )
    if "STRICT_DENOMINATIONS" in os.environ:
        cfg["strict_denominations"] = os.environ["STRICT_DENOMINATIONS"].lower() in ("1", "true", "yes")
    else:
        cfg.setdefault("strict_denominations", True)

    cfg.setdefault("min_bare_amount", int(os.environ.get("MIN_BARE_AMOUNT", cfg.get("min_bare_amount", 1000))))
    cfg.setdefault("max_bare_digits", int(os.environ.get("MAX_BARE_DIGITS", cfg.get("max_bare_digits", 6))))
    cfg.setdefault("currency_suffix", os.environ.get("CURRENCY_SUFFIX", cfg.get("currency_suffix", "")))
    cfg.setdefault("group_commands", os.environ.get("GROUP_COMMANDS", cfg.get("group_commands", "anyone")))

    # Deletion detection settings
    cfg.setdefault("verify_on_read", False)
    cfg.setdefault("sweep_interval_seconds", 20)
    cfg.setdefault("verify_workers", 4)
    cfg.setdefault("verify_budget_seconds", 3)
    cfg.setdefault("sweep_budget_seconds", 12)
    cfg.setdefault("verify_grace_seconds", 45)
    cfg.setdefault("fresh_window_seconds", 1800)
    cfg.setdefault("recheck_fresh_seconds", 120)
    cfg.setdefault("recheck_old_seconds", 3600)
    cfg.setdefault("stale_notice_seconds", 900)
    cfg.setdefault("poll_timeout", 50)
    return cfg
