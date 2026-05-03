# Pawcorder usability simulation — findings

**Method**: three persona-driven walkthroughs against `make demo` at iPhone 13 viewport.
- **阿嬤** (zh-TW, 65, tech-illiterate, wants to see her dog Coco)
- **Mark** (en-US, 32, owns 3 prior pet cams, impatient, skim-reader)
- **Hostile** (wrong passwords, fuzz inputs, dead URLs)

Screenshots: `_testdir/usability/screenshots/<persona>/NN-*.png`
Step-by-step traces: `_testdir/usability/reports/<persona>-trace.md`

---

## TL;DR — biggest finding

**Pawcorder is a pet camera that does not show pets.**

After login, neither the dashboard nor the Cameras page renders a single live frame, snapshot, or thumbnail. The user is told the count of cameras, the IP of each, and the disk path where clips are stored — but to actually *see Coco*, they have to manually open `http://127.0.0.1:5000` on a different port, which is documented behind a "View on phone" link that is itself just an instructions page. 阿嬤 will never get there.

Fix this one thing and the product becomes usable. Everything else below is secondary.

---

## P0 — blocks the core use case

### 1. No live preview on dashboard or `/cameras`
- **What I saw**: dashboard shows `living_room / 192.168.1.100` in plain text. `/cameras` shows the same with `Edit / Remove` actions. No image, no video, no snapshot.
- **Why it matters**: opening the app to watch the pet is THE use case. The current UX makes the pet invisible.
- **Fix**: in [admin/app/templates/dashboard.html](admin/app/templates/dashboard.html), replace the camera list block with a 2-up grid of live snapshot tiles (`<img src="/api/cameras/{id}/snapshot.jpg" />` polled every 3–5s, or an MJPEG/HLS embed). Same on [admin/app/templates/cameras.html](admin/app/templates/cameras.html).
- **Bonus**: tapping a tile should open a fullscreen live view inside the admin panel — the user should never need to know port 5000 exists.

### 2. "用手機觀看" / "Watch from your phone" link is misleading
- **What I saw**: 阿嬤 clicked it expecting live video. Got a wall of QR codes, `127.0.0.1:5000`, `127.0.0.1:8080`, Tailscale explanations, "iOS App coming soon".
- **Why it matters**: link text says **watch**, page does **configure**. 阿嬤 stopped here.
- **Fix**: rename the link in [dashboard.html](admin/app/templates/dashboard.html) to "**手機存取設定 / Set up phone access**". Keep the destination page; just stop promising video.

---

## P1 — heavy friction; plain-language rule violations

### 3. Login page leaks dev jargon (violates project's own copy rule)
- **What I saw**: "忘記密碼?查看主機上的 `.env` 或重新執行 `./install.sh`。"
- **Why it matters**: the auto-memory rule [user-facing copy avoids jargon](feedback_user_facing_copy.md) says admin UI copy must be plain-language. `.env` and `./install.sh` violate it on the very first screen.
- **Fix**: in [admin/app/templates/login.html](admin/app/templates/login.html), replace the always-visible hint with: "**忘記密碼?請聯絡幫你架設的人。**" Show the `.env` / `./install.sh` text only inside an expandable "我是安裝者" / "I installed this" disclosure.

### 4. Dashboard surfaces engineering primitives to end users
- **What I saw** ([screenshots/grandma/03-click-button.png](_testdir/usability/screenshots/grandma/03-click-button.png)):
  - Status reads `運行中 / **healthy**` — English word in a zh-TW string
  - Camera names: `living_room`, `kitchen`, `garage` — snake_case English
  - `192.168.1.100`, `192.168.1.101` — raw IPs
  - `儲存於 /var/folders/lm/9xjm8x4d1yq7q_vvphpspy0w0000gn/T/pawcorder-demo-27w8loa_/storage` — full unix path, leaks demo internals
  - "OpenAI 金鑰 / Pawcorder Pro 金鑰" — engineer terminology
- **Fix**:
  - Translate the health string in [admin/app/i18n.py](admin/app/i18n.py): `healthy` → "正常" / "All good".
  - During camera setup, suggest friendly localized names ("客廳", "廚房", "車庫" / "Living room", "Kitchen", "Garage"); never display the snake_case ID.
  - Hide IPs by default; surface as Wi-Fi/wired badge only. Put IP behind an "進階" toggle for advanced users.
  - Replace the storage path display with a friendly summary: "已存到外接硬碟,剩餘 320 GB" / "Saved to your USB drive — 320 GB free".
  - Reframe AI features card: don't ask for "金鑰", ask "想要 AI 寫寵物日記嗎?" with a single CTA that opens a guided flow.

### 5. 404 page is raw JSON
- **What I saw**: `/onboarding`, `/invite`, `/cameras/99999`, anything else → unstyled `{"detail":"Not Found"}` ([screenshots/hostile/10-goto-this-page-does-not-exist.png](_testdir/usability/screenshots/hostile/10-goto-this-page-does-not-exist.png)). Looks like the site is broken.
- **Why it matters**: any user who follows a stale link, mis-types a URL, or hits a deep-link from an old email lands on this. It says "Pawcorder is broken".
- **Fix**: register a custom 404 (and 500) handler in [admin/app/main.py](admin/app/main.py) that renders a styled template with logo, paw illustration, "找不到這個頁面 · 回首頁" / "Page not found · Back home" link. FastAPI: `app.exception_handler(StarletteHTTPException)`.

### 6. Camera names on dashboard look tappable but aren't
- **What I saw**: 阿嬤 tapped `living_room` on dashboard expecting to drill in. Nothing happened — they're not links.
- **Fix**: tied to (1). The whole row should be a link to the camera's live view. Until that page exists, at minimum link it to `/cameras` and apply hover/focus styles so it visually invites the tap.

---

## P2 — notable inconsistencies

### 7. Setup progress doesn't match the wizard
- **What I saw**: dashboard nudge says **「完成 3/7」**, but `/setup` only walks through **5** steps ([screenshots/poweruser/08-goto-setup.png](_testdir/usability/screenshots/poweruser/08-goto-setup.png)).
- **Fix**: pick one source of truth in [admin/app/onboarding.py](admin/app/onboarding.py). If the wizard owns onboarding, denominator is 5. If extra non-wizard steps exist (cameras added, pets added, etc.), they should appear *in* the wizard or the nudge should explain what the extras are.

### 8. Direct nav to `/setup` lands mid-flow at step 4
- **What I saw**: visiting `/setup` cold dropped me on **Detection** (step 4 of 5) with no context.
- **Fix**: on direct entry, start at step 1. Only resume at the last incomplete step if the user came from the dashboard "繼續設定" link (carry an explicit `?resume=1`).

### 9. Browser tab title says "儀表板" on the login page
- **What I saw**: tab title is `儀表板 · Pawcorder` even at `/login`.
- **Fix**: in [admin/app/templates/login.html](admin/app/templates/login.html) override the `{% block title %}` to "登入 · Pawcorder" / "Sign in · Pawcorder".

### 10. Two redundant dismiss buttons on the setup nudge
- **What I saw**: "跳過這項" + "之後再說" / "Skip this step" + "Hide for now" sit side-by-side. Same outcome to a casual reader.
- **Fix**: keep "之後再說 / Hide for now" only; remove the other.

---

## P3 — polish

### 11. "+ 新增攝影機" button overlaps the cameras page subtitle on narrow viewports
- See [screenshots/grandma/06-click.png](_testdir/usability/screenshots/grandma/06-click.png) — the orange button sits on top of "新增、編輯或移除攝影機。變更會自動套用。".
- **Fix**: in [admin/app/templates/cameras.html](admin/app/templates/cameras.html) wrap the header in a flex layout that stacks below `sm:` breakpoint.

### 12. Three controls cluster top-right (🌗 / 中文 / 登出)
- 阿嬤's screen has the brand mark, hamburger, theme toggle, language dropdown, and Sign-out — all on a 390px viewport. Theme + language could collapse into the hamburger menu.

---

## What worked well (do more of this)

1. **The setup wizard itself is excellent** — clean step indicator, "Recommended" badge on the Balanced sensitivity option, plain-language tooltips ("抓得多,會有少數誤判"), explicit "you can change this later". This is the design quality bar the dashboard should match.
2. **Locale auto-detection works** — when browser is `en-US`, login + dashboard render in English. No manual switch needed.
3. **Wrong-password handling is clean** — `/login?error=invalid` shows "密碼錯誤" without leaking timing info or echoing the attempted password back.
4. **Decorative paw icons are SVG-based** — follows the [global decorations](feedback_global_decorations.md) rule. They look on-brand in both locales.

---

## Suggested fix order (highest ROI first)

1. **Add live snapshot thumbnails to dashboard** — biggest single UX win; restores the core use case. (P0 #1)
2. **Custom 404 page** — one handler, fixes a swathe of dead-link impressions. (P1 #5)
3. **Translate `healthy` + replace unix path display + hide IPs** — three string-level changes, kills a lot of the "looks unfinished" feeling. (P1 #4)
4. **Login copy split (.env hint behind disclosure)** — single template change, makes the first screen friendly. (P1 #3)
5. **Rename "用手機觀看" → "手機存取設定"** — one string. Sets correct expectation. (P0 #2)
6. **Reconcile 3/7 vs 5-step wizard** — needs a small audit of `onboarding.py`. (P2 #7)
7. **Make camera rows linkable + add a real live-view page** — bigger lift; the live thumbnails alone may unblock 80% of users. (P0 #1 follow-up, P1 #6)

---

## How to re-run

```bash
# Start demo (separate terminal)
cd /Users/xup/workspace/pawcorder
make demo

# Re-run any persona
cd _testdir/usability
node scripts/harness.mjs grandma   scripts/plans/grandma.json
node scripts/harness.mjs poweruser scripts/plans/poweruser.json
node scripts/harness.mjs hostile   scripts/plans/hostile.json
```

Plans are JSON; add steps as `{ "click": "label" }`, `{ "fill": "selector", "value": "..." }`, `{ "goto": "/path" }`, `{ "note": "persona thought" }`. Each step writes a screenshot + a list of visible interactables to the trace file, so you can play any new persona without touching the harness code.
