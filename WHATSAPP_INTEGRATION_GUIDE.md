# WhatsApp Integration Guide — Camellia Artisanal Bot (Railway Deployment)

This is the complete, start-to-end record of connecting this bot's WhatsApp webhook
(`/webhook/whatsapp` in [app.py](app.py)) to a real WhatsApp Business number through Meta's
Cloud API, written from an actual live integration session on Railway. It covers both
onboarding paths Meta offers (manual System User setup, and Embedded Signup), the two
places a number can silently fail to receive messages even when everything "looks"
configured, and the phone-number migration flow required when the number is already
active on a normal phone.

If you only need the fast path and nothing goes wrong, follow **Part 1**. If Meta blocks
you with "phone number already registered" or "app in development mode," those are
covered in **Part 3** and **Part 4** — they are not bugs in this codebase, they are Meta
platform rules.

---

## Part 0 — What you're actually wiring together

```
Customer's phone (WhatsApp)
        │
        ▼
Meta WhatsApp Cloud API  ──(webhook POST)──▶  https://<your-railway-domain>/webhook/whatsapp
        │                                              │
        │                                     handled by app.py → whatsapp_adapter.py
        │                                              │
        └───(reply via Graph API, using WHATSAPP_ACCESS_TOKEN)◀── responder.py's answer
```

Two independent things have to work:
1. **Inbound**: Meta must be able to reach your Railway URL and forward message events (webhook verification + subscription).
2. **Outbound**: your bot must be able to call Meta's Graph API to send replies (access token + phone number ID).

Nearly every failure mode below is one of these two halves being silently unconfigured
while the other half looks fine.

---

## Part 1 — Manual System User setup (the direct path)

Use this if the number isn't currently active on a physical phone's WhatsApp app, or
you're setting up a brand-new number.

### 1.1 Create the Meta app

1. [developers.facebook.com/apps](https://developers.facebook.com/apps/) → **Create App** → type: **Business**.
2. Name it (e.g. `Camellia WhatsApp Bot`), add your contact email.
3. On the App Dashboard, under **Add Products to Your App**, find **WhatsApp** → **Set Up**.
4. Choose or create a **Meta Business Portfolio** when prompted (via [business.facebook.com/settings](https://business.facebook.com/settings) if you need to create one — takes ~1 minute, needs email verification).

### 1.2 Add and verify the phone number

1. **WhatsApp → API Setup** in the left sidebar.
2. Step 5, **Add a Phone Number** → enter Business Display Name, Category (e.g. `Food & Beverage`), the number with country code.
3. Verify via SMS or voice call OTP.
4. Copy the **Phone Number ID** shown after verification (a long numeric ID — *not* the raw phone number, *not* the WABA ID). This is `WHATSAPP_PHONE_NUMBER_ID`.

> If Meta responds with **"This phone number is already registered to a WhatsApp account"** instead of letting you verify — stop here and go to **Part 4 (Migration)**. Do not try to force it through this flow.

### 1.3 Generate a *permanent* access token

The temporary token shown on the API Setup page **expires in 24 hours** and will silently
break the bot overnight. Always use a System User token instead:

1. [business.facebook.com/settings](https://business.facebook.com/settings/) → **Users → System Users → Add**.
2. Name: `WhatsApp Bot System User`, Role: **Admin**.
3. **Assign Assets → Apps** → select your app → **Full Control**.
4. **Generate New Token** → select the app → Expiration: **Never** → permissions:
   - `whatsapp_business_messaging`
   - `whatsapp_business_management`
5. Copy the token immediately (shown once). This is `WHATSAPP_ACCESS_TOKEN`.

### 1.4 Set environment variables — in Railway, not `.env`

**Railway does not read your local `.env` file.** Set these in your Railway service's
**Variables** tab:

```env
WHATSAPP_MOCK_MODE=false
WHATSAPP_ACCESS_TOKEN=<permanent System User token from 1.3>
WHATSAPP_PHONE_NUMBER_ID=<Phone Number ID from 1.2>
WHATSAPP_API_VERSION=v19.0

META_VERIFY_TOKEN=<any string you invent — must exactly match what you type into Meta later>
META_APP_SECRET=<App Settings → Basic → App Secret>

PUBLIC_BASE_URL=https://<your-railway-domain>.up.railway.app
HUMAN_ESCALATION_PHONE=+91XXXXXXXXXX
```

Get your Railway domain from: Railway dashboard → your service → **Settings → Networking
→ Public Networking** (generate one if none exists yet).

Save the variables, then **redeploy** — adding a variable doesn't always restart the
running instance automatically depending on your Railway settings, and the code default
for `META_VERIFY_TOKEN` will silently apply if the redeploy hasn't happened yet (see
Part 5, Pitfall #1).

### 1.5 Point Meta's webhook at Railway

1. **WhatsApp → Configuration → Webhook → Edit**.
2. **Callback URL**: `https://<your-railway-domain>.up.railway.app/webhook/whatsapp`
3. **Verify Token**: exactly whatever you set `META_VERIFY_TOKEN` to in Railway.
4. **Verify and Save**. This should go green immediately if the token matches.
5. **Webhook Fields → Manage** → subscribe to **`messages`**.

> This step alone is not enough — see Part 5, Pitfall #2 (per-account subscription).

### 1.6 Subscribe the app to the WhatsApp Business Account (WABA)

App-level webhook config (step 1.5) and WABA-level subscription are **two separate
settings** in Meta's system. Missing this is the single most common reason messages
verify fine but never actually arrive.

1. Go to [business.facebook.com/wa/manage](https://business.facebook.com/wa/manage) (WhatsApp Manager — a different console from the developer app dashboard).
2. Select your WhatsApp Business Account.
3. **Configuration** (or **Webhooks**) tab → confirm your app is listed as **subscribed**. If not, subscribe it.

### 1.7 Go Live

Meta only delivers real (non-test) webhook events — including messages from you, admins,
or testers — while the app is in **Live** mode. In **Development** mode, nothing gets
through no matter how correctly everything else is set up.

1. **Settings → Basic**, fill in required fields:
   - **App Icon**
   - **Privacy Policy URL**: `https://<your-railway-domain>.up.railway.app/privacy` (already served by this app)
   - **Category**
   - **Terms of Service URL** (if shown): `https://<your-railway-domain>.up.railway.app/terms`
2. Toggle the **"In development" → "Live"** switch near the top of the App Dashboard.
3. Standard WhatsApp Cloud API messaging generally doesn't require full Meta App Review at this stage — Basic Settings + Live toggle is usually enough. If prompted for **Business Verification** (via Meta Business Suite), that's a one-time identity check for the business account, unrelated to anything in this codebase.

### 1.8 Test end-to-end

Send `"hi"` from a personal phone to the client's WhatsApp number, then check Railway's
logs (**Deployments → View Logs**) for:

```
2026-08-25 ... INFO [camellia.whatsapp] WhatsApp adapter processing incoming message from +91...
INFO:     ... "POST /webhook/whatsapp HTTP/1.1" 200 OK
```

and confirm the reply actually arrives on the test phone.

---

## Part 2 — Embedded Signup (the alternate path)

Meta also offers a guided **Embedded Signup (ES)** flow, reachable at a URL like:

```
https://developers.facebook.com/apps/<APP_ID>/whatsapp-business/es-integration/?business_id=<BUSINESS_ID>
```

This is a legitimate Meta-hosted page (real domain: `developers.facebook.com`) — it's a
different, also-valid way to connect a WABA + phone number, generating the WABA ID and
access token for you through a popup wizard instead of the manual steps in Part 1.

**When to use it**: if you don't already have a working manual setup, or the manual
"Add Phone Number" flow keeps failing and you want Meta's guided wizard to detect and
handle number migration automatically.

**When to skip it**: if you already have a working `WHATSAPP_ACCESS_TOKEN` and
`WHATSAPP_PHONE_NUMBER_ID` from the manual flow (Part 1) — you don't need to redo
onboarding through ES as well. Landing on this page while looking for something else
(e.g. the migration option) is a common accidental detour.

**On the ES page itself**:
- There are two ways to proceed: a **"Log in and Connect"** (or "Start"/"Continue with
  Facebook Login") button that launches the real guided wizard — use this.
- There's also a **manual entry section** ("Use custom WABA ID and access token") — this
  is only for pasting in credentials you *already generated elsewhere*. Don't type
  anything into it unless you already have those values from Part 1.
- The guided wizard, when launched, opens a popup that walks through selecting your
  Facebook Business, WABA, and phone number, including OTP verification and automatic
  migration detection if the number is already on a physical phone's app.
- On completion, it hands you a WABA ID and access token — set those into Railway's
  `WHATSAPP_ACCESS_TOKEN` / equivalent variables the same way as Part 1.4.

---

## Part 3 — "App is in development mode" (blocks all real messages)

**Symptom**: webhook verifies fine (green checkmark), `messages` field is subscribed,
server is reachable — but sending a real WhatsApp message produces nothing in the logs,
and Meta's UI eventually shows:

> "Apps will only be able to receive test webhooks sent from the app dashboard while the
> app is in development mode. No production data, including from app admins, developers
> or testers, will be delivered unless the app is in live mode."

**Fix**: this is exactly Part 1.7 (Go Live). Verification succeeding and Development
Mode are unrelated — verification just confirms the URL/token handshake; Development
Mode blocks *all* real traffic regardless.

---

## Part 4 — "Phone number already registered to a WhatsApp account" (migration)

**Symptom**: adding the phone number in API Setup fails with:

> "This phone number is already registered to a WhatsApp account. To continue, migrate
> this phone number or disconnect it from the existing account. Then, return to this
> page and try again. It can take up to 3 minutes for the number to refresh."

This means the number is currently logged into the regular WhatsApp or WhatsApp Business
App on a physical phone. Meta does not allow a number to be simultaneously active on the
consumer app and Cloud API.

### Decide: migrate or delete

**If the number is currently live and customers message it on the phone right now** —
use **migrate**, not delete. Migration preserves the verified/business profile status and
doesn't require wiping the app's local data.

**If it's a fresh/unused number**, or the app instance isn't actually in active use —
plain deletion is simpler: on the phone, WhatsApp (or WhatsApp Business App) →
**Settings → Account → Delete My Account**, wait ~3 minutes, then retry Part 1.2.

### How to migrate

1. **On the phone**, in the WhatsApp Business App: **Settings → Business tools →
   "Connect to Business Platform"** (exact wording varies by app version/OS — look for
   anything mentioning "Business Platform" or "Cloud API").
   - ⚠️ **Do not** tap **"Linked Devices"** — that's WhatsApp Web/Desktop pairing, a
     completely different feature, and leads to an unrelated QR scanner.
   - The correct "Connect to Business Platform" screen *also* shows a QR scanner, but
     it is a different mechanism: the counterpart QR code is generated on the **browser
     side** (Meta's Embedded Signup / API Setup page), not from another phone. You scan
     the browser's QR with this phone screen to confirm the migration handshake.
2. **On the browser**, open the phone-number step again in API Setup (Part 1.2) or the
   Embedded Signup wizard (Part 2) — it should now detect the pending migration and show
   a QR code or a **"Migrate"** option instead of the block message.
3. Scan / confirm, then complete the same SMS/call OTP as a normal add-number flow.
4. Once verified, registration transfers server-side to Cloud API. The WhatsApp Business
   App on the phone shows that number as logged out — it stops sending/receiving through
   the app from this point on.

### Is it reversible?

Yes. Meta supports migrating back (App → phone) the same way in reverse — reinstall/log
into the WhatsApp Business App with that number, and it offers a "migrate back" option
with another OTP confirmation. Notes:
- There's a brief window where the number can't send/receive anywhere during the switch.
- Meta limits how frequently a number can be migrated — don't plan on flipping back and
  forth as a routine workflow.
- The phone number, its verified status, and business profile all survive either
  direction.
- Migrating back means your bot's webhook stops receiving anything until re-migrated to
  Cloud API — this codebase's `/admin` dashboard and the escalation SMS/email in
  [escalator.py](escalator.py) are the fallback for manual handling if that ever happens.

**Consequence to flag to the client before migrating**: once migrated to Cloud API,
nobody can view or reply to WhatsApp messages from that phone's app anymore — everything
routes exclusively through the bot from then on. The `/admin` dashboard's conversation
viewer and escalation notifications are the replacement for manual visibility.

---

## Part 5 — Pitfalls actually hit during this integration (in order encountered)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | Meta shows vague error: *"The callback URL or verify token couldn't be validated"* | `META_VERIFY_TOKEN` typed into Meta's UI didn't match the value actually set in Railway's Variables tab (or Railway never had the variable set, so it silently fell back to the code default in [config.py](config.py)) | Confirm the *exact* value in Railway's Variables tab first — don't assume it matches what you typed in Meta. Test directly: `curl "https://<domain>/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=test123"` should return `200` with body `test123`. |
| 2 | Verification shows green checkmark, but zero `POST /webhook/whatsapp` ever appears in logs after sending a real message | Two possible causes stacked: (a) app still in **Development Mode** (Part 3), (b) app not subscribed to the WABA itself, only at the App Dashboard level (Part 1.6) | Check Development/Live toggle first (usually the actual blocker), then confirm WABA-level subscription in WhatsApp Manager separately from the App Dashboard webhook config |
| 3 | Adding the phone number fails with "already registered" error | Number still logged into WhatsApp/WhatsApp Business App on a physical phone | See Part 4 — migrate (preferred for live numbers) or delete-and-reregister (fresh numbers only) |
| 4 | Tapping "Connect to Business Platform" shows a QR scanner, looks identical to WhatsApp Web pairing | This is actually correct — the counterpart QR is meant to be shown on the browser/Embedded Signup side, not another phone | Open the API Setup or Embedded Signup page in a browser at the same time; scan whatever QR appears there |
| 5 | A page asks to manually enter "WhatsApp Account ID and Access Token" via an "Embedded Signup flow" | Landed on Meta's ES integration page (`developers.facebook.com/apps/<id>/whatsapp-business/es-integration/`) — legitimate, but not needed if you already have working manual credentials | Confirm the domain is genuinely `developers.facebook.com` before entering anything. Use the guided "Log in and Connect" button, not the manual paste fields, unless you already generated those values yourself |
| 6 | Bot replies work for a day then silently stop | Used the 24-hour temporary token instead of a permanent System User token | Regenerate per Part 1.3, expiration **Never** |
| 7 | Images/menu cards fail to render in WhatsApp | `PUBLIC_BASE_URL` not a full public `https://` URL reachable by Meta's servers | Must be the real Railway public domain, not `localhost` or a stale ngrok URL left over from local testing |
| 8 | Outbound message to a customer returns 400 from Meta | Customer is outside the 24-hour service window, or wrong `WHATSAPP_PHONE_NUMBER_ID` | Customer must have messaged first to open the 24h window; double check the Phone Number ID (not the WABA ID, not the raw phone number) |

---

## Part 6 — Quick verification commands

Test the webhook GET handshake directly without touching Meta's UI (useful for isolating
"is my server right" from "is Meta's config right"):

```bash
curl -s "https://<your-railway-domain>.up.railway.app/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=<YOUR_META_VERIFY_TOKEN>&hub.challenge=test12345"
# Expect: 200 OK, body exactly "test12345"
```

Confirm the server is generally reachable:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "https://<your-railway-domain>.up.railway.app/health"
# Expect: 200
```

Note: you **cannot** safely simulate an inbound WhatsApp message with a plain `curl POST`
— the `/webhook/whatsapp` POST handler verifies `X-Hub-Signature-256` against
`META_APP_SECRET` ([app.py](app.py):321), and a fake unsigned payload will either be
rejected or, if you construct a valid signature, will write fake data into the live
`camellia.db` and escalation system. Always test the real inbound path with an actual
WhatsApp message from a phone.

---

*Written from a live Railway integration session for Camellia Artisanal, 2026-08-25.*
