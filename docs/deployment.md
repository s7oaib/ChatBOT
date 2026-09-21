# Deployment

The live service runs on **Railway**. The current service URL is an operator-managed
deployment value; verify it in Railway before running a production health check.

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
| `PUBLIC_BASE_URL` | the service's own `https://` URL | Menu cards are stored as relative paths and Meta fetches them by URL. Unset, every card is dropped and customers get text-only replies with no menu photos. |
| `WHATSAPP_BUSINESS_NUMBER` | digits with country code | Used only to build the manual `wa.me` link. It is not a Cloud API credential and is never used to receive or send messages server-side. |
| `BYPASS_AD_GATE` | `true` | `false` restores PRD #4: only ad-originated Instagram DMs (`referral.source == "ADS"`) get a reply and organic ones are left for a human. With it `true` the bot answers everyone, including complaints — the escalation triggers in `data/qa_templates.json` are then the only thing routing those to a human. |
| `ENABLE_WEB_TESTER` | leave unset | Defaults off. Setting it `true` exposes `/chat` to anyone with the URL, and that tester writes real conversation and handoff rows. |

## Persistent storage (required)

Without a volume, **everything the bot writes is erased on every deploy** — the
database (conversations, escalations, approved candidate answers), a menu
pasted into `/admin`, and any template approved from the review queue. The
service looks perfectly healthy throughout, which is why `/health` now reports
a `storage` block and warns when the database sits somewhere a deploy will
wipe.

### Setting it up on Railway

1. **Settings → Volumes → Add volume**, mount path `/app/var`.
2. Add these variables and redeploy:

   ```
   DB_PATH=/app/var/camellia.db
   MENU_JSON_PATH=/app/var/menu.json
   QA_TEMPLATES_PATH=/app/var/qa_templates.json
   ```

The mount path must **not** be `/app/data`. A volume there would hide the seed
`menu.json` and `qa_templates.json` that ship inside the image, and the bot
would start with nothing to say.

A fresh volume is empty, so on first boot the bot copies those seed files onto
it (`camellia/bootstrap.py`) and logs `Storage bootstrap: seeded …`. If the
volume is unavailable or read-only, the failure is logged and the bot falls
back to the copies in the image — degraded, but still answering.

**After first boot, `menu.json` and `qa_templates.json` are no longer treated
the same way — this matters if you edit either one.**

`menu.json` is written at runtime by every menu upload, so once it exists on
the volume it is never touched again by a deploy. Editing the seed copy in the
repo and redeploying does nothing; menu content only changes through the
`/admin` panel or a Google Sheet sync (see below).

`qa_templates.json` **is** reconciled on boot, but only when the seed file's
`seed_version` (a top-level integer field) is higher than the version recorded
on the volume. Editing a template's copy in `data/qa_templates.json` and
deploying **does nothing on its own** — you must also bump `seed_version`, or
the change never reaches customers. This is deliberate, not an oversight: it
is what lets a template be edited live through `POST /admin/templates` without
every routine deploy silently reverting that edit. The file this replaces is
kept alongside it as `qa_templates.superseded-<epoch>.json`, so a bump that
lands on top of a live edit is still recoverable.

Before 2026-09-04, `qa_templates.json` used the same "seed once, never again"
rule `menu.json` still uses — a bento-pricing template was edited in the repo,
committed, and deployed, but sat undelivered on the volume for two days
because nothing forced a reconciliation. `/health` now catches this class of
bug: `answering.template_seed.undelivered_seed_changes` is `true`, and a
top-level warning names the exact fix, whenever the image's seed has moved on
from what the volume was last reconciled against. An edit made through
`/admin` does **not** trip this warning — only an un-bumped repository change
does.

**To ship a template edit:**
1. Edit `data/qa_templates.json`.
2. Bump the top-level `"seed_version"` integer.
3. Commit, push, deploy.
4. Confirm via `GET /health` — `template_seed.undelivered_seed_changes` should
   read `false` and `live_seed_version` should equal `seed_version`.

(Or skip the deploy entirely and `POST /admin/templates` with `{"template":
{...}}` — same effect, no redeploy, and it does not require a `seed_version`
bump.)

Confirm it worked in `/health`:

```json
"storage": { "database": { "path": "/app/var/camellia.db", "expected_durable": true } }
```

## Menu updates — the Google Sheet is the source of truth

With the production storage contract, a menu edited through `/admin` is written
to the configured durable `MENU_JSON_PATH` (normally `/app/var/menu.json`). If
the volume is missing or the path is left inside the image, the edit is lost on
the next deploy. Do not mount a volume at `/app/data`: that would hide the seed
`menu.json` and `qa_templates.json` the bot needs to start.

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

### Uploaded flyer photos

Uploading a photo in `/admin` (Drag-and-Drop Menu Image Flyer) saves the photo
itself, not just the text OCR extracted from it — "show me the menu" then
sends that actual image instead of one of the two fixed pictures in the repo.
It is stored under `uploads/` next to `menu.json`, so it lands on the same
volume automatically with no extra configuration. **Reset to Baseline** also
clears it, reverting to the built-in Till Thursday / From Friday rotation.

The caption sent with it is deliberately generic — no prices, no dish names —
because the OCR extraction has not been reviewed by a human at that point, and
a wrong price reaching a customer is worse than no price. The accurate menu
still goes out as text, built from `menu.json`'s structured items.

## Secrets — set in the dashboard, never committed

`META_APP_SECRET`, `META_VERIFY_TOKEN`, `INSTAGRAM_ACCESS_TOKEN`,
`INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_PAGE_ID`, `WHATSAPP_BUSINESS_NUMBER`,
`ADMIN_API_KEY`, `ADMIN_PIN`, `GROQ_API_KEY`.

Instagram Business Login uses a separate, explicit credential pair:
`INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`,
`INSTAGRAM_REDIRECT_URI`, and `INSTAGRAM_EXPECTED_USERNAME`. The app ID and
secret must be copied from the same Meta app that issues the authorization
code. Do not use the old Page/webhook secret by assumption. The default
redirect URI is:

`https://web-production-0cad70.up.railway.app/auth/instagram/callback`

After setting the matching pair, sign in to Admin and open
`/auth/instagram/start`. The callback verifies the returned account and shows
only its non-secret ID and username; the access token is not stored by the
application and must be updated through the Railway dashboard/control plane.

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
