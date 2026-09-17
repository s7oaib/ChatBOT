# Camellia Artisanal — Meta Instagram Auto-Response Bot

![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![Framework](https://img.shields.io/badge/FastAPI-0.110.0-green.svg)
![Meta API](https://img.shields.io/badge/Meta%20Graph%20API-v19.0-0084FF.svg)
![Database](https://img.shields.io/badge/SQLite-3.0-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

Production-ready customer auto-response engine for **Camellia Artisanal** (a gourmet sweets & cloud kitchen brand in Delhi NCR).

Built to convert Meta Ad leads, filter out organic DM noise, send high-resolution Menu Photo Cards (`Till Thursday.jpeg`, `From Friday.jpeg`, `Signature Sondesh.jpeg`, `AAMAR MISHTI.jpeg`), and route complex or sensitive customer inquiries to human staff.

---

## 🚀 Key Business Features & Architecture

### 1. 🛡️ Instagram Ad-Gate Filter (PRD Requirement #4)
- **Ad-Originated DMs Only**: Checks Meta Webhook referral data (`messaging[0].referral.source == 'ADS'`).
- **Targeted Auto-Replies**: Responds *only* to incoming messages from paid Instagram Ads.
- **Organic DM Protection**: Organic (non-ad) DMs pass directly to the inbox for manual staff replies.

> **The live deployment runs with the gate bypassed** (`BYPASS_AD_GATE=true`),
> so the bot currently answers every Instagram DM, organic ones included. The
> behaviour described above is what `BYPASS_AD_GATE=false` restores. See
> [deployment guide](docs/deployment.md).

### 2. 🖼️ Visual Menu Photo Card Engine
- Delivers high-resolution menu photo cards directly in Instagram DMs and WhatsApp chats instead of long text lists.
- **Dynamic Weekly Menu Switch**:
  - **Mon – Thu**: Sends `Till Thursday.jpeg` (Mid-week menu card).
  - **Fri – Sun**: Sends `From Friday.jpeg` (Weekend menu card).
- **Sondesh Collections**:
  - Sends `Signature Sondesh.jpeg` or `AAMAR MISHTI.jpeg` for Sondesh price inquiries.

### 3. 🎯 100% Deterministic Keyword Q&A (PRD Requirement #5)
- Uses keyword boundary regex matching against structured Q&A templates (`qa_templates.json`).
- Eliminates AI hallucinations, incorrect pricing, and inaccurate delivery estimates.
- Covers pricing, weekly menu items, dietary preferences (Veg/Non-Veg), delivery areas (Delhi NCR), minimum order rules, and payment methods.

### 4. 🚨 Human Escalation Pipeline (PRD Requirement #4b)
- **Out-of-Syllabus Protection**: Unmatched customer queries are never answered with guesses.
- **Sensitive Trigger Guard**: Queries containing sensitive keywords (`refund`, `complaint`, `cancel order`, `custom order`, `bulk order`, `payment link`) immediately trigger human escalation.
- **Owner Notification**: Logs customer IDs, channels, and message snippets in `camellia.db`, then alerts human staff via SMS/Email.

### 5. 🔐 Kitchen & Menu Admin Portal (`/admin` / `/admin/dashboard`)
- **Secure Admin Authentication**: Browser access uses `ADMIN_USERNAME` + an Argon2id `ADMIN_PASSWORD_HASH`, production TOTP MFA, durable rate limiting, CSRF-protected server sessions, logout revocation, and a one-time MFA-protected Forgot Password flow. `ADMIN_API_KEY` remains a separate machine credential; legacy `ADMIN_PIN` is not accepted.
- **Admin Password Recovery**: `/admin/forgot-password` sends a single-use, 15-minute reset link only to the configured `ADMIN_RECOVERY_EMAIL` through the configured transactional mailer. The reset requires TOTP or an approved one-use recovery code, stores only a token hash, revokes all browser sessions, and never changes `ADMIN_API_KEY` or disables MFA. Set `ADMIN_EMAIL_PROVIDER=resend`, `ADMIN_EMAIL_FROM`, `ADMIN_EMAIL_API_KEY`, and `ADMIN_PUBLIC_BASE_URL` in the deployment secret store; the application never rewrites Railway variables.

The first v1.1 startup bootstraps the mutable admin credential from `ADMIN_USERNAME` and `ADMIN_PASSWORD_HASH` into the durable SQLite database. Later password resets update only that persisted Argon2id hash, so the current password continues working immediately after upgrade and the environment hash is not silently re-applied on every restart. If the database is replaced or the credential row is absent, startup bootstraps it again from the environment values.
- **📋 Paste Menu Card Parser**: Extracts dish names, descriptions, weights, and prices from raw WhatsApp text, flyer notes, or dish lists, automatically categorizing items as Veg or Non-Veg.
- **📸 Flyer Photo & Graphic OCR**: Scans printed menu photos or Instagram flyers with a Groq vision model, using the same key the bot already needs for replies.
- **📊 Google Sheets Live Sync**: One-click sync from Google Sheets CSV without server restarts or touching code.
- **🍱 Active Menu Viewer**: Inspect live menu items visually with an instant **Reset to Authentic Bengali Culinary Baseline**.
- **📈 Analytics & Escalations**: Tracks real-time match rates, query totals, and unresolved human escalations.

### 6. 💬 Browser-Based Live Chat Tester (`/chat`)
- Built-in WhatsApp/Instagram-style web chat interface for live testing in your browser (`http://localhost:8000/chat`).
- Interactive memory badges, channel switcher, and direct link to the Kitchen Admin Portal.

---

## 🛠️ Technology Stack

- **Backend Framework**: Python 3.11+, FastAPI, Uvicorn
- **Deterministic Matcher & Parser**: Python Regex & Rule-Based Matchers ($0 Operating Cost)
- **AI Replies & Multimodal OCR**: Groq (uses the client's own key under their own account)
- **HTTP Client**: `httpx` (Async Meta Graph API & WhatsApp Cloud API calls)
- **Database**: SQLite3 (`camellia.db`)
- **API Standards**: Meta Graph API v19.0 (Instagram Messaging API & WhatsApp Business Cloud API)

---

## 📁 Repository Structure

```
Camellia.artisal/
├── main.py                      # Process entrypoint (Docker, Railway, Render)
├── camellia/                    # Application package
│   ├── app.py                   # FastAPI app, webhooks & /admin portal
│   ├── responder.py             # Deterministic Q&A matcher & photo card engine
│   ├── whatsapp_adapter.py      # Meta WhatsApp Cloud API adapter
│   ├── ad_gate.py               # Instagram Ad-Gate filter
│   ├── menu_parser.py           # Text & image menu extraction engine
│   ├── order_calculator.py      # Order draft parsing & receipt totals
│   ├── rag_fallback.py          # Controlled retrieval over approved sources
│   ├── ai_responder.py          # Groq refinement / RAG provider
│   ├── session_memory.py        # Customer facts & conversation state
│   ├── database.py              # SQLite storage (logs, sessions, escalations)
│   ├── escalator.py             # Human escalation & alert pipeline
│   ├── sheet_sync.py            # Google Sheet CSV live menu update
│   ├── slang_normalizer.py      # Hinglish / shorthand normalisation
│   ├── monitoring.py            # Logging, delivery health & reply latency
│   └── config.py                # Environment variable loader
├── data/                        # Seed configuration loaded at runtime
│   ├── menu.json                # Structured weekly menu database
│   └── qa_templates.json        # Q&A triggers & template responses
├── images/                      # Menu photo cards served at /images
├── tests/                       # Pytest suite (conftest puts the root on sys.path)
├── pytest.ini                   # Restricts discovery to the committed test suite
├── scripts/                     # Operational tools, not shipped in the image
│   ├── validate_schemas.py      # menu.json / qa_templates.json validator
│   ├── simulate_bot.py          # Interactive CLI simulator
│   └── generate_pdf.py          # Menu PDF generator
├── docs/                        # Deployment, evaluation and architecture docs
├── Dockerfile, docker-compose.yml, railway.json, Procfile
├── requirements.txt             # Dependencies list
└── LICENSE                      # MIT License
```

See [docs/architecture.md](docs/architecture.md) for package ownership,
[docs/deployment.md](docs/deployment.md) for hosting, and
[docs/conversation-benchmark.md](docs/conversation-benchmark.md) for quality
evaluation.

> The runtime SQLite database is **not** kept in `data/`. `docker-compose.yml`
> mounts its persistent volume at `/app/var`, which would otherwise hide the
> seed JSON baked into the image.

---

## ⚡ Quick Start & Installation

### 1. Clone & Set Up Environment
```bash
git clone https://github.com/s7oaib/ChatBOT.git
cd ChatBOT/Camellia.artisal
python -m venv venv
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in your Meta App Credentials:
```env
META_VERIFY_TOKEN=camellia_verify_secret_2026
WHATSAPP_ACCESS_TOKEN=your_meta_access_token
INSTAGRAM_PAGE_ID=your_instagram_page_id
HUMAN_ESCALATION_PHONE=+918796551990
```

### 3. Run Server

```bash
uvicorn camellia.app:app --reload --port 8000
```
*(Server runs locally at `http://localhost:8000`)*

---

## 💻 Test Web Interface (`/chat`)

The bot comes with a built-in **Interactive Web Chat Tester** available at `/chat`, `/demo`, or `/test`. This provides a zero-setup visual environment to test bot responses, menu image cards, Ad-Gate filtering, and Ollama RAG locally in your browser without requiring live Meta developer credentials.

> **Off by default.** The tester writes real conversation rows and fires real escalation alerts to the owner's phone, so it returns `404` unless `ENABLE_WEB_TESTER=true` is set. Enable it in local development only — never on a public domain.

### Accessing the Web Interface

Start your server in terminal:
```bash
ENABLE_WEB_TESTER=true uvicorn camellia.app:app --port 8000
```

Then open the Web Chat Interface in your browser:
- [http://localhost:8000/chat](http://localhost:8000/chat) *(or `http://localhost:8000/demo`)*

---

### Features of the Web Chat Interface

```text
 ┌────────────────────────────────────────────────────────────────────────┐
 │ Camellia Artisanal — Web Bot Tester                                 │
 ├────────────────────────────────────────────────────────────────────────┤
 │ Channel Mode: [ 📢 Instagram Ad DM ]  [ 🛑 Instagram Organic ] [ 🟢 WA ]│
 ├────────────────────────────────────────────────────────────────────────┤
 │ Quick Test Chips:                                                      │
 │ [ 🍬 Sondesh Prices ] [ 🥬 Weekly Menu ] [ 🚚 Delivery Area ]          │
 │ [ 🦙 Ollama RAG Query ] [ 🚫 Out-of-Syllabus ] [ 🚨 Refund Escalation ]│
 ├────────────────────────────────────────────────────────────────────────┤
 │ [Bot]: 👋 Hi! Welcome to Camellia Artisanal.                           │
 │ [User]: How much is Sondesh?                                           │
 │ [Bot]: Our artisanal Sondesh starts at ₹450 per box.                  │
 │        📷 [Image Card: Signature Sondesh.jpeg]                        │
 └────────────────────────────────────────────────────────────────────────┘
```

1. **Channel Mode Selectors**:
   - **📢 Instagram Ad DM (`instagram_ad`)**: Simulates user entering via a paid Instagram Ad referral (`referral.source == 'ADS'`). Generates auto-replies + high-resolution Menu Photo Cards.
   - **🛑 Instagram Organic DM (`instagram_organic`)**: Simulates non-ad organic DMs. Tests the **Ad-Gate Filter**, displaying:
     `🛑 [AD-GATE BLOCKED]: This is an organic (non-ad) DM. The bot does not auto-reply to organic DMs. Message sits in Inbox for human reply.`
   - **🟢 WhatsApp Business (`whatsapp`)**: Simulates incoming WhatsApp Cloud API messages.

2. **Preset Quick Test Chips**:
   - **🍬 Sondesh Prices**: Triggers Sondesh pricing matrix & dispatches `Signature Sondesh.jpeg` or `AAMAR MISHTI.jpeg`.
   - **🥬 Weekly Veg Menu**: Triggers dynamic day-of-week menu logic (`Till Thursday.jpeg` vs `From Friday.jpeg`).
   - **🚚 Delivery Areas**: Tests Delhi NCR coverage rules vs outside city notifications.
   - **🦙 Ollama RAG Query**: Asks natural language questions resolved via local `llama3.2` model RAG.
   - **🚫 Out-of-Syllabus Refusal**: Tests out-of-context queries (`"What is the capital of France?"`), verifying `OUT_OF_CONTEXT` refusal + human escalation.
   - **🚨 Sensitive Refund Escalation**: Tests complaint/refund guard (`"I want a refund"`), verifying human alert logging.

3. **Live Image Preview Card Render**:
   - Automatically renders outbound menu JPEG cards (`Till Thursday.jpeg`, `From Friday.jpeg`, `Signature Sondesh.jpeg`, `AAMAR MISHTI.jpeg`) inline in the chat bubble.

---

## 📊 Google Spreadsheet Live Menu Sync (`sheet_sync.py`)

Menu items and pricing can be updated directly via a published Google Sheet CSV without modifying any code or JSON files.

### How it works

1. **Maintain Menu in Google Sheets**: Store weekly menu items, categories, descriptions, quantities, and prices in a standard Google Sheet.
2. **Publish as CSV**: In Google Sheets, select `File -> Share -> Publish to Web -> CSV`.
3. **Configure URL**: Add your CSV link to `GOOGLE_SHEET_CSV_URL` in `.env`.
4. **Trigger Live Sync**: Call the `/admin/sync-menu` endpoint via `POST /admin/sync-menu` to hot-reload `menu.json` and memory instantly.

```bash
# Example manual sync request
curl -X POST "http://localhost:8000/admin/sync-menu?csv_url=https://docs.google.com/spreadsheets/d/e/.../pub?output=csv" -H "X-Admin-Key: camellia_admin_secret_key_2026"
```

---

## 🧪 All Testing Methods Summary

### Method 1: Interactive Browser Web Interface (`/chat`)
Set `ENABLE_WEB_TESTER=true`, then open **[http://localhost:8000/chat](http://localhost:8000/chat)** in your web browser.

### Method 2: Automated Unit Test Suite (`tests/test_bot.py`)
Run all 25 automated unit tests:
```bash
python -m pytest tests/
```
*Tests:* Ad-Gate inspection, webhook challenges (`GET /webhook/*`), webhook signature rejection, WhatsApp parsing, menu photo card logic, Ollama local RAG answers, refusals, offline fallbacks, SQLite logging, retention purge, per-user data deletion, legal pages, tester lockdown, and delivery-failure alerting.

### Method 3: Interactive CLI Terminal Simulator (`scripts/simulate_bot.py`)
Run an interactive terminal chat session:
```bash
python scripts/simulate_bot.py
```
Type queries directly in your terminal, toggle Ad vs Non-Ad DMs, and inspect live database analytics and human escalations.

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](file:///LICENSE) file for details.
