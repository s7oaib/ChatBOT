# Camellia Artisanal Server Deployment Guide

This guide explains what must be deployed, whether a server is required, and how to keep credentials and customer data safe.

## Do I need to buy a server?

No physical server is required.

For local development, the bot runs on your computer. For Instagram and WhatsApp webhooks to reach it reliably, production needs a publicly reachable HTTPS service. That can be:

- A managed cloud service that runs Docker or Python.
- A small virtual private server (VPS).
- A managed container platform.

The repository already includes `Dockerfile` and `docker-compose.yml`. A VPS or managed container with persistent storage is the most predictable production setup. A free or temporary hosting plan may sleep, change its URL, or lose the SQLite database if persistent storage is not enabled.

## What is stored on the server?

The server needs the application code and approved assets:

```text
app.py
config.py
database.py
responder.py
rag_fallback.py
session_memory.py
whatsapp_adapter.py
instagram/webhook code in app.py
qa_templates.json
menu.json
images/
requirements.txt
Dockerfile
docker-compose.yml
```

The SQLite database is created at runtime and must be stored on persistent disk:

```text
/app/data/camellia.db
```

Do not copy these to GitHub or expose them publicly:

```text
.env
*.db
*.db-wal
*.db-shm
customer exports
Meta access tokens
Google service-account keys
private backups
```

The existing `.gitignore` excludes `.env` and database files.

## Stage 1: Local mock deployment

This stage does not require Meta or WhatsApp credentials.

1. Copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Keep WhatsApp mock mode enabled:

   ```env
   WHATSAPP_MOCK_MODE=true
   WHATSAPP_ACCESS_TOKEN=
   WHATSAPP_PHONE_NUMBER_ID=
   ENABLE_WEB_TESTER=true
   ```

3. Start the application:

   ```bash
   docker compose up -d --build
   ```

4. Check the health endpoint:

   ```text
   http://localhost:8000/health
   ```

5. Use the local tester at `/chat` or run:

   ```bash
   python simulate_bot.py
   ```

Mock mode processes bot logic but never sends an outbound HTTP request to Meta.

## Stage 2: Separate WhatsApp API-number pilot

Create or provide a separate business phone number intended for WhatsApp Business Platform. Do not deregister or migrate the client’s existing WhatsApp number during this stage.

Keep the server in mock mode until the number and Meta configuration are ready. When ready, add the real values only to the server’s private `.env`:

```env
WHATSAPP_MOCK_MODE=false
WHATSAPP_ACCESS_TOKEN=<real token>
WHATSAPP_PHONE_NUMBER_ID=<separate pilot number ID>
WHATSAPP_API_VERSION=v19.0
PUBLIC_BASE_URL=https://<production-domain>
```

Restart the service after changing environment variables:

```bash
docker compose up -d --build
```

Before sending real messages, verify the container logs and a controlled test message. The adapter fails closed when live mode is enabled without the required credentials.

## Stage 3: Instagram and Meta webhooks

The production service must have a stable HTTPS URL. Configure the Meta webhook callback URLs using that domain:

```text
https://<production-domain>/webhook/instagram
https://<production-domain>/webhook/whatsapp
```

Set these only in the private server environment:

```env
META_VERIFY_TOKEN=<random webhook verification token>
META_APP_SECRET=<Meta app secret>
INSTAGRAM_PAGE_ID=<Instagram/Page ID>
INSTAGRAM_ACCESS_TOKEN=<Instagram access token>
```

Do not put Meta passwords or access tokens in this repository. The client should authorize access through Meta Business Manager/Business Login.

## Persistent database and backups

With Docker Compose, the database volume is mounted at `/app/data`:

```yaml
volumes:
  - camellia_db_data:/app/data
```

Back up the database before upgrades and at least daily during the pilot. This uses Python’s built-in SQLite support, so it does not require the `sqlite3` command-line tool in the container:

```bash
docker compose exec camellia-bot python -c "import sqlite3; src=sqlite3.connect('/app/data/camellia.db'); dst=sqlite3.connect('/app/data/camellia-backup.db'); src.backup(dst); dst.close(); src.close()"
```

Copy the backup to private storage. Never commit it to GitHub or place it in a public web directory.

## Production security checklist

- Set `WHATSAPP_MOCK_MODE=false` only after the separate API number is approved.
- Set a strong `ADMIN_API_KEY` and/or `ADMIN_PIN`.
- Set `ENABLE_WEB_TESTER=false`.
- Use a stable HTTPS `PUBLIC_BASE_URL`.
- Store `.env` in the hosting provider’s secret/environment-variable manager.
- Mount persistent storage for `/app/data`.
- Restrict server administration to authorized staff.
- Monitor `/health`, webhook errors, delivery failures, and pending escalations.
- Keep the existing WhatsApp number unchanged until the pilot is accepted.

## Rollback

If the pilot has a problem:

1. Set `WHATSAPP_MOCK_MODE=true`.
2. Remove the pilot API token and phone-number ID from the server environment.
3. Restart the container.
4. Review pending escalations and delivery logs.
5. Keep the client’s original WhatsApp number available for normal business use.
