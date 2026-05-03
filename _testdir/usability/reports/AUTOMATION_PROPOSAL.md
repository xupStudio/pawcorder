# Pawcorder OSS — engineer-work elimination proposal

**Constraint**: zero monthly cost to Pawcorder team. Everything ships in OSS-tier. Where automation needs an external account, it's the **user's** account (their Google Drive, their Telegram, their Tailscale).

**Method**: 35 engineer-input touchpoints inventoried across the admin UI; for each, a zero-cost technical path. Sources cited inline.

---

## TL;DR — implementation order (highest leverage / lowest effort first)

| # | Friction | Solution | Effort | OSS-friendly? |
|---|----------|----------|--------|---------------|
| 1 | AI features need API key paste (4 vendors × paste flow) | **Auto-detect Ollama at `127.0.0.1:11434`; one-click install + model pull** | S | ✅ MIT (Ollama) |
| 2 | Tailscale hostname paste + manual install | **Auto-detect via `tailscale status --json`; one-click install button** | S | ✅ existing script |
| 3 | NAS path manual entry | **mDNS scan for `_smb._tcp.local`; pick from list** | S | ✅ python-zeroconf MIT |
| 4 | Telegram chat ID lookup | **Bot deep-link pairing: 6-digit code, user clicks `t.me/<bot>?start=PAIR-CODE`, bot captures chat_id** | M | ✅ Telegram bot API free |
| 5 | Cloud backup via rclone CLI dance | **OAuth device-code flow for Drive/OneDrive; PKCE for Dropbox; embedded Nextcloud Login Flow v2** | L | ✅ all free |
| 6 | Login forgot-password says "edit `.env`" | **File-flag reset: drop `/data/config/reset.flag`, admin offers password reset on next login** | S | ✅ |
| 7 | Uninstall = copy shell command | **Three buttons (Soft / Full / Nuke) with `DELETE` typed-confirm** | S | ✅ existing script |
| 8 | Home Assistant requires docker-compose edit + YAML paste | **One-click docker service add via `docker_ops`; auto-mint long-lived token via HA WebSocket auth API; POST automation to HA REST** | M | ✅ HA is OSS |
| 9 | LINE notifications | **Same pairing flow as Telegram** (LINE Messaging API still needs channel; no escape) | M | ⚠️ LINE has 500/mo free quota |
| 10 | ntfy.sh as new "easy notifications" default | **Generate random topic; show QR; user installs ntfy app + scans** | S | ✅ Apache 2.0 |
| 11 | OpenAI / Gemini / Anthropic key paste | **Keep as power-user paths; promote local Ollama as default. No automation possible (no consumer OAuth for API keys)** | n/a | inherent friction |
| 12 | S3 / B2 access keys | **Inherent friction (no OAuth from those providers); add deep-link buttons that pre-fill provider signup with referral context** | n/a | inherent friction |

**Recommended starting batch**: #1, #2, #3, #4, #6, #7, #10 — all S/M effort, all OSS-only, all zero ongoing cost. Together they kill ~20 of the 35 touchpoints.

---

## One-time setup needed from xup (free, no monthly cost)

These are **prerequisite OAuth app registrations** — each takes 5–15 min to set up once, no ongoing charges.

| Provider | Why | Cost | Verification needed? |
|----------|-----|------|----------------------|
| Google Cloud Console | OAuth client for Google Drive backup, scope = `drive.file` | $0 forever | **No** — `drive.file` is non-sensitive ([source](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)) |
| Microsoft Identity Platform (Azure AD) | OAuth client for OneDrive, scope = `Files.ReadWrite.AppFolder` | $0 personal | No — app folder scope is OK |
| Dropbox developer | OAuth client for Dropbox, type = "App folder" | $0 | No |
| Apple developer | (Optional) iOS app for ntfy / mobile shell | **$99/yr** — skip for now, use ntfy iOS app instead |

The OAuth client_id and client_secret get bundled in OSS code (semi-public, mitigated by PKCE + device-code flow) — same as how Tailscale, rclone, and other OSS clients ship them.

If xup doesn't want to do these registrations, items #5 (Cloud backup) stays manual. Everything else above the line works without any external account from Pawcorder team.

---

# Detailed proposals

## 1. AI / Local LLM — auto-detect Ollama

### Current pain
`/system` page asks for OpenAI / Gemini / Anthropic API key OR Ollama URL + model name. Even Ollama (the local option) currently requires the user to type:
- `http://127.0.0.1:11434` (URL — they'd have to know default port)
- `qwen2.5:3b` (model — they'd have to know what models exist)

### Proposed solution

**Layer 1 — silent detection** (smallest change):
- On admin start, probe `GET http://127.0.0.1:11434/api/tags`. If 200, set defaults for URL + first available model.
- Add a green "Ollama detected — using local AI" badge on `/system`.

**Layer 2 — one-click install**:
- "Install local AI" button on `/system` runs:
  - macOS / Linux: `curl -fsSL https://ollama.com/install.sh | sh` (Ollama's official installer, MIT)
  - Windows: download `OllamaSetup.exe` and trigger user-confirmation install (or document via the existing WSL2 path)
- Stream installer logs into the page so the user sees progress.

**Layer 3 — model picker by RAM**:
- Read `/proc/meminfo` (or `psutil.virtual_memory()`) → recommend:
  - **<4 GB**: `qwen2.5:0.5b` (~400 MB, basic)
  - **4–8 GB**: `qwen2.5:3b` (~2 GB, recommended)
  - **>8 GB**: `qwen2.5:7b` (~4.5 GB, best)
- "Pull model" button calls `POST http://127.0.0.1:11434/api/pull` with streaming progress.

### Library / deps
- No new Python deps; just `httpx` calls to `127.0.0.1:11434`.
- Ollama itself is **MIT** ([github.com/ollama/ollama](https://github.com/ollama/ollama)).
- Models: Qwen 2.5 = Apache 2.0, Llama 3.2 = Llama community license (commercial OK <700M MAU), Phi 3.5 = MIT.

### Effort: **S** (~1 day) — detection probe + install button + model picker.

### Eliminates
- 4 paste fields: OpenAI key, Gemini key, Anthropic key, Pro license key (they all become "advanced — only if Ollama isn't enough")
- 2 paste fields: Ollama URL, Ollama model name (auto-filled)

**Net: 6 → 0 touchpoints in the AI area** (the keys still exist as power-user paths, but the default is one-click).

---

## 2. Tailscale — auto-detect + one-click install

### Current pain
`/mobile` page shows shell commands the user must run on the host:
```
./scripts/install-tailscale.sh
sudo tailscale up
```
Then asks them to paste their Tailscale hostname (`pawcorder.tailXXX.ts.net`).

### Proposed solution

**Auto-detect**:
- On admin load, run `tailscale status --json` (subprocess, 1s timeout).
- If output parses: extract `Self.DNSName`, auto-fill the hostname field, hide the install instructions.
- If exit ≠ 0: show "Tailscale not running — [install]" button.

**One-click install**:
- Button calls a new endpoint `POST /api/tailscale/install` that subprocess-runs the existing `scripts/install-tailscale.sh`.
- On success, runs `tailscale up --json` which returns an auth URL → display as a clickable link.
- After user signs in, re-run `tailscale status --json` and auto-fill.

**Sign-in URL passthrough**:
- `tailscale up` outputs a URL like `https://login.tailscale.com/a/abc123` — extract via regex from stdout, render as a big "Open Tailscale to sign in" button.

### Library / deps
- None new; subprocess + `tailscale status --json` (already on the box if installed).
- Tailscale CLI is **BSD-3** ([github.com/tailscale/tailscale](https://github.com/tailscale/tailscale)).
- Free for personal use up to 100 devices ([source](https://tailscale.com/pricing)).

### Effort: **S** (~half day) — wraps existing script, adds detection probe.

### Eliminates
- 3 touchpoints: install command, sign-in command, hostname paste.

---

## 3. NAS storage — mDNS discovery

### Current pain
`/storage` lets the user manually type a SMB path like `//192.168.1.5/share`. Test + auto-mount buttons exist but only after the path is typed.

### Proposed solution

Background scan on `/storage` page load:
- `python-zeroconf` (LGPL, OK as runtime dep for OSS) browses for `_smb._tcp.local`, `_afpovertcp._tcp.local`, `_nfs._tcp.local`.
- Discovered devices appear as a list: `Synology — DS920+ (192.168.1.42)`, `TrueNAS — fileserver (192.168.1.50)`.
- User clicks one → path auto-fills, prompts for credentials → `STORAGE_NAS_TEST` button proceeds.

### Library / deps
- `zeroconf>=0.131` (LGPL-2.1 — runtime dep allowed for OSS distributions). Already `zeroconf` is a battle-tested option used by Home Assistant.

### Effort: **S** (~half day) — scanner + UI list.

### Eliminates
- 1 touchpoint: NAS path manual entry. Credentials still needed (inherent — SMB/NFS doesn't have OAuth).

---

## 4. Telegram — bot deep-link pairing for chat_id

### Current pain
Two paste fields on `/notifications`:
- Bot token (12-digit, copied from BotFather)
- Chat ID (numeric, requires user to message a 3rd-party bot like `@userinfobot` to obtain)

### Proposed solution

**Token**: still pasted once (unavoidable — Telegram has no OAuth for "this user authorizes this bot"; the user must create a bot themselves, and the token is the bot's identity).

**Chat ID**: fully eliminated via deep-link pairing.

Flow:
1. User pastes their bot token, clicks "Connect".
2. Pawcorder starts polling on the new bot, generates a random pairing code (e.g., `pcr-abc123`).
3. UI shows a QR code + clickable link: `https://t.me/<bot_username>?start=pcr-abc123` (`bot_username` came from `getMe` API call).
4. User clicks → Telegram opens → "/start pcr-abc123" sent to bot → Pawcorder's poller receives the update → captures `update.message.chat.id` + matches against the pending pairing code → saves chat_id.
5. UI shows "✓ Connected to @username (chat 1234567)".

This is a standard pattern — `start` parameter accepts up to 64 chars `[A-Za-z0-9_-]` ([Telegram deep-link spec](https://core.telegram.org/api/links#bot-links)).

### Library / deps
- Existing `python-telegram-bot` already does polling. Add ~50 lines for pairing-code state machine.

### Effort: **M** (~1 day including the pairing-code expiry/cleanup logic).

### Eliminates
- 1 of the 2 Telegram touchpoints (chat_id). Bot token paste is inherent.

**Same pattern works for LINE**: LINE has equivalent `https://line.me/R/oaMessage/<channel_id>/?<msg>` flow. But LINE Messaging API also caps at 500 free messages/month — fine for personal use, but worth a callout.

---

## 5. ntfy.sh — alternative "no-token" notifications

### Current pain
Even after the Telegram pairing flow above, the user is committed to creating their own bot. Some users won't bother. We need a "literally zero-config" path.

### Proposed solution

ntfy.sh — a FOSS push-notification service. No accounts, no tokens, no bots. Just topics:
1. Pawcorder generates a random unguessable topic name: `pawcorder-{random_36chars}`.
2. UI shows: "Install the **ntfy** app on your phone, scan this QR" — QR encodes `https://ntfy.sh/pawcorder-xyz`.
3. ntfy app subscribes; phone now receives push whenever Pawcorder POSTs to that topic.
4. Pawcorder POSTs notifications: `curl -d "Mochi spotted in kitchen" https://ntfy.sh/pawcorder-xyz`.

**Done. No tokens. No bots. Two clicks (install app + scan QR).**

### Constraints (verified)
- Free tier: 60 messages burst, 1 per 5s replenish ([source](https://docs.ntfy.sh/faq/)) — way more than personal pet alerts need
- iOS push: works via the public ntfy.sh's APNs relay, even when self-hosted ([source](https://noted.lol/ntfy/))
- Topic is the secret; anyone with the URL can subscribe AND publish, so:
  - Use 32+ random chars
  - **Encrypt message bodies** with a key shared only between Pawcorder and the ntfy app config (ntfy supports E2E for self-hosted; for ntfy.sh public instance, do app-layer AES-GCM in Pawcorder)
- For higher volume / business use, ntfy has paid tiers — irrelevant here

### Self-hosting option
Add `ntfy` to `docker-compose.yml` as an optional service (one extra container, ~20 MB). Switch from public `ntfy.sh` to `http://ntfy:80` internally. Zero external dependency.

### Library / deps
- No SDK needed — `httpx.post()` with a body. ntfy is HTTP.

### Effort: **S** (~half day for the basic flow + QR + encryption).

### Eliminates
- All notification setup if user picks ntfy. **0 paste fields, 0 tokens, 0 bots.**
- Telegram + LINE remain as power-user options.

---

## 6. Cloud backup OAuth — Drive / OneDrive / Dropbox

### Current pain (worst flow in the whole product)
`/cloud` instructs users to:
1. Install rclone on their **personal computer** (not the Pawcorder host)
2. Run `rclone authorize "drive"` in a terminal
3. Sign in to Google in a browser that opens
4. Copy a JSON token blob from the terminal
5. Paste the JSON blob into Pawcorder

Every step an opportunity to give up.

### Proposed solution: **OAuth Device Code flow** (RFC 8628)

This is what Apple TVs / Roku / consoles use. Zero callback URL gymnastics:

1. User clicks "Connect Google Drive" in Pawcorder.
2. Pawcorder calls Google's device endpoint:
   ```
   POST https://oauth2.googleapis.com/device/code
   client_id=<pawcorder_oauth_client>
   scope=https://www.googleapis.com/auth/drive.file
   ```
3. Google returns:
   ```json
   {
     "device_code": "...",
     "user_code": "ABCD-EFGH",
     "verification_url": "https://google.com/device",
     "expires_in": 1800
   }
   ```
4. Pawcorder displays a big card: "Visit **google.com/device** on any device, enter **ABCD-EFGH**". Plus QR code that goes straight to `verification_url?user_code=ABCD-EFGH`.
5. Pawcorder polls `POST https://oauth2.googleapis.com/token` every 5s with the `device_code` until Google returns access + refresh token.
6. Pawcorder saves refresh token in rclone config; rclone uploads work as before.

**User saw 1 button + entered 1 short code on phone. Zero terminal, zero JSON paste.**

### Per-provider mapping

| Provider | Method | Notes |
|----------|--------|-------|
| Google Drive | Device code | `drive.file` scope = non-sensitive, no app verification ([source](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)) |
| OneDrive | Device code | `Files.ReadWrite.AppFolder` scope; supported by MS identity platform |
| Dropbox | PKCE flow w/ embedded loopback (Dropbox doesn't support device code) | Need short-lived `127.0.0.1:RANDOMPORT` callback during setup |
| Nextcloud / ownCloud | Login Flow v2 | Returns app password, no real password ever in Pawcorder ([Nextcloud docs](https://docs.nextcloud.com/server/stable/developer_manual/client_apis/LoginFlow/index.html)) |
| iCloud | None — Apple has no public Drive API | **Not feasible**, document as "not supported" |
| S3 / B2 / WebDAV | No OAuth — keys remain pasted | Inherent friction; mitigation: deep-link to provider signup |

### Prerequisites (one-time, free)
- Register OAuth client at Google Cloud Console (free, no monthly cost, `drive.file` scope skips verification — only "App Verification" needed which is fast)
- Register at MS Identity Platform (free)
- Register at Dropbox developer (free)

The client_id is bundled in OSS code; client_secret too (PKCE makes leaked secret useless).

### Library / deps
- New: `httpx` for token exchange (already a dep), `qrcode` for verification QR (already a dep). No new deps.

### Effort: **L** (~3 days, mostly QA per provider)
- Drive flow: 1 day
- OneDrive flow: 0.5 day (very similar to Drive)
- Dropbox flow: 1 day (PKCE + loopback handler is fiddlier)
- Nextcloud Login Flow v2: 0.5 day

### Eliminates
- Drive/OneDrive: 3 paste touchpoints → 1 button + 1 short code typed on phone
- Dropbox: 3 → 1 button (PKCE handles internally)
- Nextcloud: 3 → 1 button + sign-in
- S3 / B2 / WebDAV: unchanged (no OAuth available — fundamental)

---

## 7. Login recovery — file flag

### Current pain
`/login` "I'm the installer" disclosure says "open `.env` on the host or re-run `./install.sh`". Both require shell access. Most family members don't have it.

### Proposed solution

Replace with a **file-flag reset**:
- User SSHes / file-managers into the data dir, creates an empty file: `/data/config/.reset-password`
- On next visit to `/login`, admin detects the file → renders an inline "Set a new password" form (one field, no auth required because file presence proves shell access).
- After password is set, deletes the flag file.

This is no less secure than today (anyone who can edit `.env` can also create this flag). It's just discoverable.

UI on `/login`:
```
Forgot the password? Ask the installer.
▼ I'm the installer
   To reset: create an empty file at /data/config/.reset-password
   on the Pawcorder host. Then refresh this page.
```

### Library / deps
- None.

### Effort: **S** (~2 hours).

### Eliminates
- 1 touchpoint (vague "open .env" → concrete reset path).

---

## 8. Uninstall — buttons inside admin

### Current pain
The /uninstall page tells users to copy a shell command (Soft / Full / Nuke). Risky to copy-paste from a web page.

### Proposed solution

Three buttons, each requiring user to type "DELETE" to confirm:

```
[ Soft uninstall — keep recordings & settings ]
[ Full uninstall — keep recordings only      ]
[ Nuke — delete everything                   ]

Type "DELETE" to confirm: [______________]
```

Each button hits a new admin endpoint that subprocess-runs the existing `uninstall.sh` with the appropriate flag. The catch: full/nuke kill the admin itself, so:
- Spawn the script in a detached process with `setsid`
- Admin returns 200 + immediate redirect to a "uninstall in progress" splash
- Script kills the admin + tears down docker-compose

### Library / deps
- None — existing `uninstall.sh` already covers all three modes.

### Effort: **S** (~half day).

### Eliminates
- 1 touchpoint (copy-paste shell command).

---

## 9. Home Assistant — auto-add + auto-token

### Current pain
`/home-assistant` page asks user to:
- Copy a docker-compose snippet
- Run `docker compose up -d homeassistant`
- Copy an automation YAML
- Paste it into HA's `configuration.yaml`

### Proposed solution

**Step 1 — auto-add HA service**:
- "Add Home Assistant" button calls `POST /api/ha/install` which:
  - Reads `/data/docker-compose.yml`, appends the HA service block
  - Runs `docker compose up -d homeassistant` via existing `docker_ops.py`
  - Polls until `http://homeassistant:8123` responds

**Step 2 — auto-mint long-lived token**:
- HA has an [Authentication API](https://developers.home-assistant.io/docs/auth_api/) with WebSocket command `auth/long_lived_access_token`
- Pawcorder uses HA's IndieAuth-compatible OAuth flow:
  1. Open `http://homeassistant:8123/auth/authorize?client_id=http://pawcorder/&redirect_uri=...`
  2. User signs into HA, approves
  3. Pawcorder exchanges code → access token → uses WebSocket to mint long-lived token
  4. Stores long-lived token

**Step 3 — push automation directly**:
- POST automation to `http://homeassistant:8123/api/config/automation/config/<id>` with the `Authorization: Bearer <long_lived_token>` header
- Or post via WebSocket `config/automation/create`

### Library / deps
- New: a thin HA client (~200 lines) — or vendor `python-homeassistant-api` (MIT).

### Effort: **M** (~1.5 days).

### Eliminates
- 3 touchpoints: docker YAML copy, docker run command, MQTT automation YAML.

---

## 10. LINE notifications — pairing flow + caveat

### Current pain
LINE Messaging API requires:
- Channel access token (from LINE Developers Console)
- Target user ID / group ID (extracted from webhook log)

### Proposed solution

Same pattern as Telegram (#4):
- User pastes channel access token once (unavoidable — LINE doesn't have user-OAuth-for-bots)
- Pawcorder generates a pairing code, displays a `https://line.me/R/oaMessage/<channel_id>/?text=pcr-abc123` link
- User clicks → LINE opens → user sends "pcr-abc123" → Pawcorder webhook captures `userId`
- Done

### Caveat

LINE Messaging API free tier = **500 messages/month**. For typical pet alerts (a few per day), this is fine. For aggressive users, they'd hit the cap.

Recommend **promoting ntfy.sh as primary** in copy ("Most users want ntfy — LINE is for those who already use it for everything").

### Effort: **M** (~1 day, similar to Telegram).

### Eliminates
- 1 of 2 LINE touchpoints (target ID).

---

## 11. Inherent friction — kept as-is

These can't be automated without Pawcorder team running infrastructure or paying Apple/Stripe/etc:

- **OpenAI / Gemini / Anthropic API keys**: no consumer OAuth for API keys exists. Mitigation: Ollama default (#1) makes these power-user-only.
- **S3 / B2 access keys**: no OAuth from those providers. Mitigation: deep-link to signup flows so user doesn't need to read docs.
- **Mobile app DIY build**: requires Xcode + Apple Developer ($99/yr) for iOS. Cannot automate. Document as "advanced — only if you're already an iOS dev".
- **Pawcorder Pro license key**: Pro feature, not in this OSS batch.

---

## What this batch ships

If we do **#1, #2, #3, #4, #6, #7, #10** (the recommended starting batch):

- **35 → ~12 touchpoints** (66% reduction)
- **Average new-user setup time**: from ~30 min (read 3 setup pages, paste 6 things) to ~3 min (click 5 buttons, scan 2 QRs)
- **Net new code**: ~600–800 lines across 7 small features
- **External cost to Pawcorder team**: $0/mo + ~30 min one-time OAuth client registrations (skippable if we drop #5)

If we add #5 (cloud OAuth) and #9 (HA): another ~3 days of work, kills another 12 touchpoints.

If we add #11 (inherent items): re-mark them clearly as "advanced — paste needed" rather than presenting them as primary.

## Decision points for xup

Before I implement, three questions:

1. **Is `/data/config/.reset-password` an acceptable login-recovery path?** Same security as `.env`, but does require physical/SSH access.
2. **OK with bundling Pawcorder OAuth client_id+secret in OSS source?** It's standard practice (rclone, Tailscale, etc. all do this) but worth a confirm.
3. **Promote ntfy.sh as the default notification path?** Means new users see ntfy first, Telegram + LINE collapse to "Other options". Tradeoff: ntfy is less familiar in Asia than LINE.

If yes to all three, I'll build #1, #2, #3, #4, #6, #7, #10 in one batch followed by 5 rounds of review (per memory rule).

---

## Sources

- [Choose Google Drive API scopes — drive.file is non-sensitive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Sensitive scope verification — drive.file exempt](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification)
- [ntfy.sh FAQ — rate limits + iOS push details](https://docs.ntfy.sh/faq/)
- [Self-hosted ntfy + iOS push relay (Noted blog)](https://noted.lol/ntfy/)
- [Nextcloud Login Flow v2 — appPassword without real password](https://docs.nextcloud.com/server/stable/developer_manual/client_apis/LoginFlow/index.html)
- [Telegram bot deep links — start parameter spec](https://core.telegram.org/api/links#bot-links)
- [Home Assistant Authentication API — long-lived access tokens](https://developers.home-assistant.io/docs/auth_api/)
