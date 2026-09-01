# Deployment

The live service runs on **Railway**, at `https://chatbot-production-8164.up.railway.app`.

Build and start config lives in [`railway.json`](railway.json) (`main.py` via
Nixpacks). Railway has no committed environment file — **every variable below is
set in the Railway dashboard only**, so nothing in this repository will tell you
when one is missing or wrong. `/health` is the authority on what is actually
live; check it after any change.

> A `render.yaml` used to sit alongside this file and was deleted, because it
> described a Render service that does not exist. It was actively harmful: it
> declared `ENABLE_WEB_TESTER=false`, which read like the tester was closed in
> production while Railway never read that file at all.

## Variables that decide whether customers are served

These fail quietly. The process stays up and `/health` reports `healthy`, so the
only signal is the `warnings` array and the fields below it.

| Variable | Set to | What happens if it is wrong |
|---|---|---|
| `WHATSAPP_MOCK_MODE` | `false` | In mock mode the bot never builds an outbound request. It accepts webhooks, matches, logs a reply, and sends nothing — customers get silence while the process looks perfectly healthy. |
| `PUBLIC_BASE_URL` | the service's own `https://` URL | Menu cards are stored as relative paths and Meta fetches them by URL. Unset, every card is dropped and customers get text-only replies with no menu photos. |
| `BYPASS_AD_GATE` | `true` | `false` restores PRD #4: only ad-originated Instagram DMs (`referral.source == "ADS"`) get a reply and organic ones are left for a human. With it `true` the bot answers everyone, including complaints — the escalation triggers in `data/qa_templates.json` are then the only thing routing those to a human. |
| `ENABLE_WEB_TESTER` | leave unset | Defaults off. Setting it `true` exposes `/chat` to anyone with the URL, and that tester writes real conversation rows and fires real escalation alerts at the manager's phone. |

## Menu updates — the Google Sheet is the source of truth

The container filesystem does not persist. A menu edited through `/admin` is
written to `/app/data/menu.json` **inside the container** and is gone on the
next deploy, when the copy baked into the image comes back. No volume is
mounted there, and mounting one would hide the seed `menu.json` and
`qa_templates.json` the bot needs to start.

So the durable path is a published Google Sheet, which the bot re-pulls on
every boot. Whatever the client last saved in the sheet is what customers are
quoted, however often the service restarts.

### Setting it up

1. Create a Google Sheet with **exactly these column headers** in row 1:

   | Item | Description | Price_INR | Quantity | Category |
   |---|---|---|---|---|
   | Kosha Mangsho | Slow-cooked mutton | 520 | 300g | Non-Veg |
   | Shukto | Mixed vegetable classic | 280 | 250g | Veg |

   `Price_INR` must be a plain number — no `₹`, no commas, or the row is
   skipped with a warning. Any `Category` containing "non" is treated as
   non-vegetarian; everything else is vegetarian.

2. **File → Share → Publish to web** → choose the sheet → format
   **Comma-separated values (.csv)** → **Publish**. Copy the generated URL.
   This must be the *published* CSV link, not the normal `/edit` address — a
   sharing link returns HTML and the sync will reject it.

3. Set `GOOGLE_SHEET_CSV_URL` in Railway to that URL and redeploy.

Only HTTPS URLs on Google domains are fetched; anything else is refused, so the
setting cannot be pointed at an internal address.

### Using it

- **On every deploy** the sheet is pulled automatically. A sheet that is
  offline or malformed is logged and skipped — the bot still starts and answers
  from the menu in the image.
- **Immediately after editing the sheet**, press **Sync** in `/admin`
  (`POST /admin/sync-menu`) to pull it without waiting for a redeploy.

The sync replaces the items in the most recent `weekly_menus` block. Sondesh
collections and prices live in `collections` and are not touched by it.

## Secrets — set in the dashboard, never committed

`META_APP_SECRET`, `META_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_PAGE_ID`,
`ADMIN_API_KEY`, `ADMIN_PIN`, `GROQ_API_KEY`.

Once `META_APP_SECRET` is set and `/health` reports `webhook_signature:
"enforced"`, also set `REQUIRE_WEBHOOK_SIGNATURE=true` so unsigned payloads are
rejected outright rather than merely unverified.

## Verifying a deploy

```
curl -s https://chatbot-production-8164.up.railway.app/health
```

A healthy production response has `reply_delivery.mode` of `"live"`,
`webhook_signature` of `"enforced"`, `ai.provider` of `"groq"`, and an empty
`warnings` array. `ai.working` stays `null` until the first AI call — that is
normal on a fresh deploy, not a fault.

Environment changes do not reach a running container. Redeploy after editing
any variable, or the old value stays live.
