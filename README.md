# 💰 Telegram Tally Bot

![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Zero-Dependencies](https://img.shields.io/badge/Dependencies-Standard%20Library%20Only-green.svg)
![License](https://img.shields.io/badge/License-MIT-orange.svg)

A lightweight, **zero-dependency**, **zero-LLM** Telegram group amount tallying bot with robust numeral parsing, phone reference tracking, message deletion detection, and strict business rule validation.

---

## 🌟 Key Features

- **🔢 Flexible Numeral & Amount Parsing**: Reads amounts like `5K`, `10K`, `15,000`, `25k`, `10K`, `25,000 MMK`, and Myanmar numerals (`၁၀K`, `၂၅,၀၀၀ ကျပ်`).
- **🎯 Strict Allowed Denominations**: Accepts only configured amounts (`5K`, `10K`, `15K`, `20K`, `25K`). Warns users if they send out-of-range amounts.
- **🔗 Reply-Only Enforcement**: Guarantees accurate accounting by only tallying amounts sent as replies to valid phone or reference numbers.
- **📱 Phone & Reference Normalization**: Smart resolution for phone numbers (`09...`, `9...`, partial quote selections like `675362816` vs `09675362816`).
- **🛡️ Duplicate Prevention**: Prevents counting the same reference number multiple times within the same local day.
- **🔄 Message Deletion Detection**: Probes message existence in parallel background threads using invisible reactions, keeping the ledger self-healing.
- **📊 Real-time Summaries & Pagination**: Clean HTML reports with `/total`, `/details`, and interactive paginated `/list`.
- **⚡ Zero External Dependencies**: Built entirely using Python's Standard Library (`urllib`, `json`, `re`, `threading`, `concurrent.futures`, `pathlib`).

---

## 📁 Directory Structure

```text
tally/
├── .env.example              # Environment variables template
├── .gitignore                # Git ignore rules
├── LICENSE                   # MIT license
├── main.py                   # Primary application entrypoint & CLI runner
├── tally.py                  # Backward-compatible wrapper
├── README.md                 # Documentation
├── state/                    # Local persistent state (ignored in git)
│   └── tally.db              # SQLite database (entries, offset, control)
├── tests/                    # Stdlib-only self-test suite (main.py --self-test)
│   └── test_main.py
└── src/                      # Source package
    ├── core/
    │   ├── config.py         # Zero-dependency .env loader, paths, TZ
    │   └── ledger.py         # SQLite ledger (WAL), legacy JSON auto-import
    ├── parser/
    │   └── amount_parser.py  # Regex matching, phone normalization, reference resolution
    └── telegram/
        ├── client.py         # Bot API client, deletion probe & background sweep
        └── handlers.py       # Command dispatchers, inline pagination, HTML views
```

---

## 🚀 Quickstart

### 1. Clone the repository

```bash
git clone https://github.com/your-username/tally.git
cd tally
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` and fill in your Bot Token and Owner ID:

```ini
TALLY_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
OWNER_IDS=123456789
ALLOWED_CHAT_IDS=-1001234567890
REQUIRE_REPLY=true
STRICT_DENOMINATIONS=true
ALLOWED_DENOMINATIONS=5000,10000,15000,20000,25000
```

### 3. Run the Bot

```bash
# Run unit checks and self-tests
python3 main.py --self-test

# Start the long-polling bot daemon
python3 main.py --run
```

---

## ⚙️ Environment Variables Reference (`.env`)

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `TALLY_BOT_TOKEN` | `string` | *Required* | Telegram Bot API Token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_CHAT_IDS` | `list` | `[]` | Comma-separated group chat IDs. Leave empty to allow all chats |
| `OWNER_IDS` | `list` | `[]` | Comma-separated Telegram User IDs for bot owners |
| `COUNT_ONLY_OWNER` | `bool` | `true` | If true, only tallies messages sent by `OWNER_IDS` |
| `REQUIRE_REPLY` | `bool` | `true` | Only counts amounts that reply to a reference/phone message |
| `STRICT_DENOMINATIONS` | `bool` | `true` | Strictly limits allowed values to `ALLOWED_DENOMINATIONS` |
| `ALLOWED_DENOMINATIONS` | `list` | `5K,10K,15K,20K,25K` | Allowed denominations (e.g. `5000,10000,15000,20000,25000`) |
| `MIN_ALLOWED_AMOUNT` | `int` | `5000` | Minimum allowed amount if strict mode is disabled |
| `MAX_ALLOWED_AMOUNT` | `int` | `25000` | Maximum allowed amount if strict mode is disabled |
| `CURRENCY_SUFFIX` | `string` | `""` | Optional currency label (e.g., `MMK`, `Ks`) |
| `GROUP_COMMANDS` | `string` | `anyone` | Who can run commands: `anyone` or `owner` |

---

## 🤖 Bot Commands Reference

| Command | Description |
| :--- | :--- |
| `/total` | Shows today's total amount and message count |
| `/total YYYY-MM-DD` | Shows total for a specific date (e.g., `/total 2026-08-22`) |
| `/details` | Breakdown grouped by denomination (e.g., `25K — 10 items`, `10K — 5 items`) |
| `/list` | Chronological listing with inline **Next / Prev** pagination |
| `/search <ref>` | Search by phone number or reference code (e.g., `/search 09672`) |
| `/verify` | Run an on-demand message deletion probe sweep |
| `/dayclose` | *(Owner only)* Lock a day's ledger as an immutable snapshot |
| `/dayopen` | *(Owner only)* Reopen a closed day's ledger |
| `/maintenance` | *(Owner only)* Temporarily pause tallying across all chats |
| `/active` | *(Owner only)* Resume tallying after maintenance |
| `/help` | Display command usage guide |

---

## 💾 Storage

All state lives in a single SQLite file (`state/tally.db`, WAL mode):

| Table | Contents |
| :--- | :--- |
| `entries` | One row per tallied message; PK `(chat_id, message_id)`; day-indexed for fast reports & duplicate checks |
| `meta` | Telegram update offset + migration flag |
| `control` | Maintenance flag and closed days |

Writes commit immediately (no full-file rewrites), and edits/deletions are reconciled by primary key.
If a legacy `state/ledger.json` / `offset.json` / `control.json` exists from an older version, it is imported once on first start — the original files stay untouched as a backup.

---

## 🚢 Production Deployment

### Option 1: Fly.io (Recommended Cloud Deployment)

Deploy the bot seamlessly to [Fly.io](https://fly.io) with persistent SQLite storage and zero idle downtime.

#### 1. Install & Login to Fly CLI
```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Log in to your Fly account
fly auth login
```

#### 2. Create a Unique App
Fly application names are globally unique across all accounts:
```bash
fly apps create <your-unique-app-name>
```

#### 3. Configure `fly.toml`
Update the `app` name in `fly.toml`:
```toml
app = "<your-unique-app-name>"
primary_region = "sin"     # Singapore — low latency to Telegram & Myanmar

[build]
  dockerfile = "Dockerfile"

[env]
  TZ = "Asia/Yangon"

# Persistent SQLite volume (state/tally.db) survives deploys & restarts
[mounts]
  source      = "tally_data"
  destination = "/app/state"

[vm]
  size   = "shared-cpu-1x"
  memory = "256mb"
```

> [!NOTE]
> There is intentionally **no `[http_service]`** section in `fly.toml` because the bot operates as an outbound long-polling worker (`getUpdates`). Do not add HTTP ports or health checks.

#### 4. Create Persistent Storage Volume
Create an encrypted 1GB volume in the Singapore region (`sin`):
```bash
fly volumes create tally_data --region sin --size 1
```

#### 5. Set Telegram Secrets
Securely inject your environment variables into Fly Secrets (never commit secrets to git or docker image):
```bash
fly secrets set \
  TALLY_BOT_TOKEN="your_bot_token_here" \
  OWNER_IDS="123456789" \
  ALLOWED_CHAT_IDS="-1001234567890"
```

#### 6. Deploy to Fly
```bash
fly deploy
```

> [!IMPORTANT]
> A Telegram long-polling bot must only run **exactly 1 machine instance** to prevent duplicate message polling and Telegram `409 Conflict` errors:
> ```bash
> fly scale count 1
> ```

#### 7. Useful Management Commands
```bash
# View live streaming logs
fly logs

# Check application status & machine health
fly status

# Restart the bot daemon
fly apps restart <your-unique-app-name>

# Deploy latest code updates
fly deploy
```

---

### Option 2: Linux Systemd (VPS / Dedicated Server)

To run the bot as a background service on Linux:

1. Create a service file:

```bash
sudo nano /etc/systemd/system/tally.service
```

2. Add the configuration:

```ini
[Unit]
Description=Telegram Tally Bot Daemon
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/tally
ExecStart=/usr/bin/python3 /home/your_username/tally/main.py --run
Restart=always
RestartSec=5
EnvironmentFile=/home/your_username/tally/.env

[Install]
WantedBy=multi-user.target
```

3. Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tally
sudo systemctl status tally
```

---

## 🧪 Testing

Run the built-in test suite:

```bash
python3 main.py --self-test
```

---

## 📄 License

This project is open source and available under the [MIT License](https://opensource.org/licenses/MIT).
