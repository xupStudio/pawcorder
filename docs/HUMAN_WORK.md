# Human-Required Work — items code can't finish on its own

This doc tracks everything in Pawcorder that needs **human action** to
complete: vendor signups, hardware testing, real-world validation,
visual / creative work, threshold tuning that needs live data, and
manual policy decisions.

The memory rule "After every feature batch, update HUMAN_WORK.md if
needed" keeps this list fresh — when a coding session lands work that
*depends* on something only a human can do, append it here so it isn't
silently forgotten.

Format: each section is a category, each item is one row in
`Status / Item / Why human / Next step`.

Last updated: 2026-05-02 (Batch 7: PWA + Android hardening — manifest
modernised (id / shortcuts / orientation any / maskable PNGs / proper
192-512 raster fallbacks), service-worker dvh / bilingual offline,
Capacitor Pawcorder cap + Android channel config, dashboard three-state
recording status + diagnostic banner + last-event time + PWA install
+ push pre-prompt, mobile.html shell-command de-jargon + Android battery
hint, welcome.html 6 next-step cards, errors.py + /api/diagnostics, family
invite link flow (invites.py + /invite/<token>), marketing free-vs-Pro
comparison table).

---

## Vendor signups & API keys (BYOK)

| Status | Item | Why human | Next step |
|---|---|---|---|
| ⏳ Open | **Cartesia** API key | Sign up at cartesia.ai, listen to voice demos, pick voice UUIDs | Update `CARTESIA_VOICE_DEFAULTS` in [relay/tts.py](../relay/tts.py) — current zh-TW / en / ja IDs are placeholders that may not exist on the live account |
| ⏳ Open | **ElevenLabs** API key | Sign up, choose default + multilingual voice IDs, optionally clone a brand voice | Update `ELEVENLABS_VOICE_DEFAULTS` env override |
| ⏳ Open | **Google Gemini** API key | Get key from AI Studio, pick billing account | `PAWCORDER_RELAY_GEMINI_KEY` (relay) or `GEMINI_API_KEY` (admin BYOK) |
| ⏳ Open | **Anthropic Claude** API key | Sign up at console.anthropic.com, enable prompt caching | `PAWCORDER_RELAY_ANTHROPIC_KEY` (relay) or `ANTHROPIC_API_KEY` (admin BYOK) |
| ✅ Done | **OpenAI** API key | Existing | Already wired |
| ✅ Done | **Stripe** webhook secret | Already wired in `relay/stripe_webhook.py` | — |

## Hardware-dependent

| Status | Item | Why human | Next step |
|---|---|---|---|
| ⏳ Open | **Hailo-8L AI HAT+** integration | Needs Pi 5 + Hailo card on-desk + Hailo SDK licence | Compile ONNX→HEF; add `docker-compose.linux-hailo.yml`; YOLOv11 + MobileNetV3 both need conversion |
| ⏳ Open | **Coral / Edge TPU** path | Needs Coral USB stick on-desk | Frigate already supports — just needs SKU validation with real cameras |
| ⏳ Open | **GPU host** for ElevenLabs / Cartesia / XTTS-v2 | XTTS-v2 self-hosted option needs a GPU box reachable from the relay | Stand up a small server (RunPod / Modal / on-prem); set `PAWCORDER_RELAY_XTTS_URL` |
| ⏳ Open | **Bluetooth adapter** on Pawcorder host | Wireless-onboarding BLE scanner needs a working BLE radio. Most mini-PC / NUC / RPi 5 builds have it built in; some headless servers don't. | Verify `bleak` reports an adapter (`bluetoothctl list` on Linux); add a USB BLE dongle (~US$ 8) if missing |
| ⏳ Open | **Second Wi-Fi NIC** on Pawcorder host (recommended) | SoftAP-mode camera provisioning hops the host onto the camera's setup AP, then back to home Wi-Fi. With one NIC the admin Wi-Fi session briefly drops; with two it's seamless. | Add a USB Wi-Fi adapter (~US$ 12) if the host is on Wi-Fi; wired-Ethernet hosts don't need this |
| ⏳ Open | **Real-device validation** of each wireless provisioner | Code for Foscam / Dahua / D-Link HNAP / generic ESP32 SoftAP / Reolink-XML QR / EspTouch v2 / WPS / Matter / HomeKit detection is implemented from public specs and reverse-engineered references; each protocol needs a real camera in pairing mode to confirm end-to-end | One camera per provisioner: Foscam C2 / Dahua IPC-HFW / TL-SC3171 (D-Link HNAP era) / ESP32-CAM dev board / Reolink Argus 3 / any EspTouch-v2 board / Aqara G3 (HomeKit) / Tapo Matter cam |
| ⏳ Open | **TPM 2.0 master-key path** validation | `master_key.py` falls back to OS keyring then file when TPM isn't available; the TPM branch (Linux + `tpm2-pytss` + `/dev/tpmrm0`) needs a TPM-equipped host to verify seal/unseal round-trips and persistent-handle survival across reboots | Run `pytest -m tpm` on a host with TPM enabled (most modern x86 NUC / Intel mini-PCs); confirm `master_key.describe_active_backend()` reports `"tpm"` |

## Live-data validation (thresholds, regressions)

| Status | Item | Why human | Next step |
|---|---|---|---|
| ⏳ Open | **MAD anomaly threshold = 3.5** | Iglewicz/Hoaglin recommendation, but pet visit counts may have heavier tails | A/B test alert false-positive rate on real households for 4 weeks; tune `DROP_ANOMALY_THRESHOLD` in [bowl_monitor.py](../admin/app/pro/bowl_monitor.py) if needed |
| ⏳ Open | **Multi-frame cap = 6** | Empirical sweet spot for indoor cam footage; not validated on our actual customer footage | Log `frames_used` distribution from production sightings, see if 6 is the right knee |
| ⏳ Open | **Cloud-boost cap = ±0.08** | Conservative but possibly too small to break ties on same-breed multi-pet households | Roll out, monitor swap-error rate on multi-pet customers, tune up to ±0.12 if safe |
| ⏳ Open | **Bowl monitor 40% backstop arm** | Prevents missed alerts on high-variance baselines but might over-fire on legitimately low-baseline pets (kittens, picky eaters) | Watch the alert log for 2 weeks; relax to 30% or remove if false-positive rate climbs |
| ⏳ Open | **MegaDescriptor benchmark vs MobileNetV3** | Research says +20-70 pp on cross-dataset re-ID; "best for our actual customers" needs measurement | Once MegaDescriptor lands as an opt-in backbone, compare match-quality on N customer photo sets |
| ⏳ Open | **DINOv2-small backbone validation** | Batch 2 added it as an opt-in via `PAWCORDER_EMBEDDING_BACKBONE=dinov2_small`. Theory says better than MobileNet on multi-pet same-breed setups; needs measurement | Pilot on a multi-tabby household, click "Re-enroll all photos" on System page, measure swap-error rate vs MobileNet |
| ⏳ Open | **MegaDescriptor ONNX export** | The HF model is PyTorch only. ONNX export is mechanical (`torch.onnx.export` on the Swin-L backbone) but needs a human with the source model + correct input signature | Add a third row to `_BACKBONES` in `embeddings.py` + host the ONNX somewhere reachable |
| ⏳ Open | **Conformal anomaly tuning** | Batch 2 added conformal_p_value with 14-day minimum history. Real customer data may need more / less history before the calibration is meaningful | After 6 weeks of production data, sample 50 conformal verdicts and compare against operator judgment of "actually unusual"; tune `CONFORMAL_MIN_HISTORY` if needed |
| ⏳ Open | **LR head with real negatives (Oxford-IIIT-Pet)** | Batch 2 deliberately did NOT add an LR head with synthetic negatives — random unit vectors don't represent the "other pet" distribution and would degrade the prototype-based classifier | Either: (a) embed the Oxford-IIIT-Pet 7000-image dataset against MobileNet, ship as a 16 MB binary asset alongside the relay, then add `_train_lr_head()` in `cloud_train_kernel.py`. (b) Wait until enough customer data accrues to use anonymised same-cohort negatives. |

## Pose-based behavior detection

| Status | Item | Why human | Next step |
|---|---|---|---|
| ✅ Done (interim) | **Bbox-based behavior chips** | Coarse resting / pacing / active labels from existing bbox stream — no model needed | Surfaced on /pets/health (batch 2). Validate the thresholds against real-customer footage and tune. |
| ⏳ Open | **YOLOv11-pose / RTMPose pre-trained for pets** | Current public pose models are mostly human-trained; pet performance varies | Download top candidate models, run on 100 frames, pick the one that lands keypoints reliably |
| ⏳ Open | **Pose-based behavior rules** | Rules like "hind legs flexed + head down ⇒ scratching" need a human to spec, code can implement | Watch own pet for 1 hour, write 4-6 rule signatures, encode in `pro/pose_behavior.py` |
| ⏳ Open | **Wire pose-derived chips into /pets/health** | Batch 4 added `admin/app/pose_scaffold.py` with `is_available()` + a `_RULES` registry. When a real pose model + rules land, the page needs to render new `BEHAVIOR_BADGE_*` keys (scratching / grooming / etc.) and `label_explanation()` should grow defaults for them. Without this, the scaffold rots silently | After model lands: extend `pets_health.html` "Behavior chip" block to call `pose_scaffold.is_available()` and surface the dominant rule label; add `BEHAVIOR_BADGE_SCRATCHING` etc. to `i18n.py`; extend `label_explanation` defaults in `behavior.py` |
| ⏳ Open | **Vomit / seizure detection** — NOT IMPLEMENTED | Liability risk if false-negatives; needs vet sign-off and curated training clips | **Do not promise in marketing.** Defer until partnered with a vet research group |

## Per-pet model training (auto when owners upload)

| Status | Item | Why human | Next step |
|---|---|---|---|
| ✅ Done | Owner upload flow | Drag-drop UI + consent + Pro relay encrypted-at-rest pipeline live | — |
| ⏳ Open | First-customer validation of cloud_train V2 model | Need ≥ 1 real customer with photos to verify the prototype + Gaussian classifier predicts correctly | Onboard a Pro pilot, watch their `petclf-<pet>.joblib` get written, check recognition gives sensible probabilities |

## i18n coverage

| Status | Item | Why human | Next step |
|---|---|---|---|
| ✅ Done | en + zh-TW for all UI | — | — |
| ⏳ Open | ja + ko full coverage | Pre-existing partial coverage in `_JA_KO_STARTER` block; new strings (SYS_GEMINI_KEY_*, SYS_TTS_PREF_*, etc.) only have en + zh-TW | Translator rounds for the 30+ new keys |

## Marketing & visual

| Status | Item | Why human | Next step |
|---|---|---|---|
| 🟡 Partial | Updated screenshots for [marketing/index.html](../marketing/index.html) | Batch 4 added `scripts/screenshot-marketing.py` (playwright-driven). Run it after starting the demo to get base captures of dashboard / pets / health / system / recognition. Designer still owns hero composition + final crop | `playwright install chromium && python scripts/screenshot-marketing.py` |
| ⏳ Open | Pricing page copy for new providers | We added Gemini / Anthropic / Cartesia / ElevenLabs — owners may want a "what does each cost?" comparison | Marketing copywriting |
| ⏳ Open | Onboarding video for /pets/<id>/train-cloud | Drag-drop flow is live but no video walkthrough | Record 90-second screencast |
| ✅ Done | Self-host Fraunces + Geist + JetBrains Mono | Batch 4 mirrored the woff2 files to `admin/app/static/fonts/` + `marketing/fonts/`. ~200 KB total. Templates load `./fonts/fonts.css` instead of the Google Fonts CDN | — |
| ⏳ Open | Marketing screenshots after Batch 3 polish | Page now uses Fraunces / Geist / paper-warm palette and refers to features by visible UI labels — old screenshots are stale | Designer round captures fresh hero / features / cameras / pricing screens |
| ⏳ Open | OG / social-share image (og:image) | Marketing index has `og:title` + `og:description` but no preview image; share previews on Twitter / iMessage / Discord look bare | Export a 1200×630 PNG of the new logo + Pawcorder wordmark on warm-paper background; upload to `/marketing/og.png` and add `<meta property="og:image">` |
| ✅ Done | Favicon raster fallbacks | Batch 7 added [scripts/build-pwa-icons.py](../scripts/build-pwa-icons.py) (qlmanage / rsvg-convert + Pillow). Outputs `icon-192.png`, `icon-512.png`, `icon-maskable-512.png` (40% safe zone for Samsung circular mask), `apple-touch-icon-180.png`. Wired into `base.html` + `manifest.json`. | — |
| ⏳ Open | Final review of editorial typography on real CJK + EN strings | Marketing & admin upgraded to Fraunces (display) + Geist (body). Need a non-zh-TW reviewer to confirm the warm-paper palette + Fraunces tracking doesn't feel "off" in en / ja / ko | Visit /login, /, /pets after switching language; check letter-spacing isn't too tight on long English/Japanese strings |
| ✅ Done | WSL2 mirrored networking note for Windows users | The wizard now detects WSL2 in-app: when `/api/scan` returns 0 cameras, `setup_helpers.detect_environment_quirks()` checks `/proc/version` and surfaces `SCAN_NO_HITS_WSL_HINT` (i18n.py) explaining the `.wslconfig` `networkingMode=mirrored` fix. Manual-IP entry still works as escape hatch. | — |

## Mobile / PWA real-device validation (Batch 7)

| Status | Item | Why human | Next step |
|---|---|---|---|
| ⏳ Open | **PWA install banner** on real Android Chrome | `beforeinstallprompt` requires HTTPS, valid manifest, and Chrome's engagement heuristic (visit, scroll, ~30 s). On localhost the event fires; on a real LAN install behind self-signed TLS Chrome may suppress it. | Install Pawcorder on a real Android phone, browse to the admin via Tailscale (HTTPS), confirm the inline install button appears within ~5 minutes of normal use |
| ⏳ Open | **Maskable icon on Samsung circular launcher** | I sized the safe zone at 40% per W3C spec but Samsung One UI's circular mask sometimes clips a touch more aggressively than vanilla Android | Install on a Samsung A / S phone, screenshot the launcher icon, verify the paw is fully visible. If clipped, rerun `scripts/build-pwa-icons.py` after dropping `inner_size` to 50% |
| ⏳ Open | **Web Push end-to-end** on Android Chrome | The push-permission pre-prompt code calls `Notification.requestPermission()` after the soft button; FCM delivery to the SW push handler requires VAPID key + correctly registered subscription | Real Android phone: enable push from dashboard banner → trigger a pet event → confirm system notification arrives even when Chrome is backgrounded; check OPPO/Xiaomi/Samsung battery-saver hint actually fires |
| ⏳ Open | **Capacitor Android push channel** validation | `bootstrap.js` calls `PushNotifications.createChannel({ id: 'pawcorder-events', importance: 4 })` but this only takes effect after `npx cap add android` has been run AND the host has Firebase SDK initialised with `google-services.json` | Run `npx cap add android` in `mobile/`, drop the `google-services.json` from the FCM console into `mobile/android/app/`, build APK, sideload on real Android, send a test push and confirm it lands as a heads-up notification |
| ⏳ Open | **iOS Capacitor APNs** validation | Same as Android but iOS path: needs Apple Developer team ID + APNs auth key + `npx cap add ios` | Apple Developer Program account ($99/year), APNs auth key, sideload via Xcode, confirm push lands |
| ⏳ Open | **Apple Developer Program enrolment** | macOS `.pkg` signing + Capacitor iOS app distribution both need an Apple Developer account ($99/year) — without it, end users see "unidentified developer" warnings on macOS and cannot install the iOS app | Enrol at developer.apple.com → request APNs auth key + macOS Developer ID Application certificate |
| ⏳ Open | **Verified Google OAuth for cloud backup (Drive)** | Pawcorder's rclone path currently asks the user for a `client_id` / `client_secret` they have to mint themselves in Google Cloud Console — non-technical users cannot do this. Verified OAuth gives a one-click "Sign in with Google" flow. | (a) Apply for OAuth verification at console.cloud.google.com (Sensitive scope: drive). (b) Pass CASA Tier 2 audit (US$ 75-2000). (c) Update `cloud.py` to use the Authorization Code Flow instead of expecting client_id/secret from the user. ETA 2-3 months. |
| ⏳ Open | **LINE Official Account** for in-app LINE notifications | `users.html` invite flow uses `https://line.me/R/share?text=...` for sharing — works fine. But the `NOTIF_LINE` channel currently asks the user to mint their own LINE Messaging API token. A "scan our official account" path needs a verified LINE OA + relay endpoint that fans out per-user notifications. | Register a LINE Official Account, enable Messaging API, build a small relay that registers users by webhook signature, update `line.py` admin path to recognise "official account" mode |
| ⏳ Open | **Diagnostics threshold tuning** (`/api/diagnostics`) | Disk-full warns at 5% free / errors at 2% free; camera-offline detection currently only flags Frigate-down (per-camera reachability not yet wired). Real-customer data may want different thresholds. | Run for 4 weeks, scan logs for false-positives, consider widening to 8% warn / 3% error. Add per-camera offline detection by tailing Frigate's MQTT or polling each camera's `/onvif/device_service` |
| ⏳ Open | **Family invite link UX field test** | The flow works end-to-end in tests, but needs a real-world try with the LINE share button — does the LINE preview render the URL? Does iOS Safari trust the URL with no warning? Does the recipient hit the redeem page and complete in < 60 s? | Generate one invite, send to a friend / partner via LINE, time them, fix any UX rough edges they hit |

## Hosted services (production deploys)

| Status | Item | Why human | Next step |
|---|---|---|---|
| ⏳ Open | Relay deploy with the new `numpy + onnxruntime + Pillow` deps | Image size grows ~150 MB; cloud-train kernel pulls these in lazily | Verify deploy succeeds on the prod relay container; smoke-test `/v1/cloud-train/upload` |
| ⏳ Open | Embedding model bootstrap on the relay | First training run downloads MobileNetV3 from HuggingFace; could fail in restricted networks | Pre-warm the cache at deploy time or bundle the 10 MB ONNX with the image |

---

## Process

When a coding session ends and you find anything in this list could
have been done in code but needed a human action you didn't have:

1. Add a row to the right table.
2. Mark `Status` as ⏳ Open.
3. Make the `Next step` concrete enough that the human reading it
   later (maybe yourself in two weeks) can act without re-discovering
   the context.
4. When the human action lands, flip to ✅ Done and update the
   commit / config / file path it changed.

Dead rows that no longer apply can be moved to a "Resolved" archive
section or deleted — better to keep this list short and live than
exhaustive and stale.
