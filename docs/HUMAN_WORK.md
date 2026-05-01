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

Last updated: 2026-05-01 (Batch 3: marketing + admin design polish — Fraunces /
Geist typography, paper-warm palette, paw-+-lens logo).

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
| ⏳ Open | Updated screenshots for [marketing/index.html](../marketing/index.html) | The page now mentions health-overview / vet-pack-share / weekly-digest features that need fresh screenshots | Designer round |
| ⏳ Open | Pricing page copy for new providers | We added Gemini / Anthropic / Cartesia / ElevenLabs — owners may want a "what does each cost?" comparison | Marketing copywriting |
| ⏳ Open | Onboarding video for /pets/<id>/train-cloud | Drag-drop flow is live but no video walkthrough | Record 90-second screencast |
| ⏳ Open | Self-host Fraunces + Geist + JetBrains Mono | Batch 3 loads from Google Fonts. China / corporate networks blocking it fall through to system fonts. ~200 KB total | Mirror the woff2 files to `admin/app/static/fonts/`; rewrite the `<link>` to `/static/fonts/...` |
| ⏳ Open | Marketing screenshots after Batch 3 polish | Page now uses Fraunces / Geist / paper-warm palette and refers to features by visible UI labels — old screenshots are stale | Designer round captures fresh hero / features / cameras / pricing screens |
| ⏳ Open | OG / social-share image (og:image) | Marketing index has `og:title` + `og:description` but no preview image; share previews on Twitter / iMessage / Discord look bare | Export a 1200×630 PNG of the new logo + Pawcorder wordmark on warm-paper background; upload to `/marketing/og.png` and add `<meta property="og:image">` |
| ⏳ Open | Favicon raster fallbacks | The new SVG icon ([admin/app/static/icon.svg](../admin/app/static/icon.svg)) is the canonical mark, but older clients (IE / older Android browsers / iMessage previews) need raster sizes | Generate `favicon-32.png`, `apple-touch-icon-180.png`, `android-chrome-192/512.png` from the SVG; reference them in admin `base.html` and marketing `index.html` |
| ⏳ Open | Final review of editorial typography on real CJK + EN strings | Marketing & admin upgraded to Fraunces (display) + Geist (body). Need a non-zh-TW reviewer to confirm the warm-paper palette + Fraunces tracking doesn't feel "off" in en / ja / ko | Visit /login, /, /pets after switching language; check letter-spacing isn't too tight on long English/Japanese strings |

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
