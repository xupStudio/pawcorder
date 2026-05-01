# Integrations

How to talk to pawcorder from other systems. Two flavours:

- **API keys** for read/write programmatic access (Home Assistant, scripts, custom apps).
- **Webhooks** for low-latency event delivery (Frigate is already wired this way; you can reuse it).

## Get an API key

In the admin: **System → Power-user features → API keys → Create**.

You'll see the plain key exactly once. Copy it — the server only stores
the SHA-256 hash. If you lose it, revoke and generate a new one.

The key looks like `pwc_AbCdEf...`. Use it as a Bearer token in the
`Authorization` header.

## curl

```sh
curl -H "Authorization: Bearer pwc_AbCdEf..." \
     http://pawcorder.local:8080/api/status
```

Every authenticated route works, e.g.:

```sh
# List pets
curl -H "Authorization: Bearer $KEY" \
     http://pawcorder.local:8080/api/pets

# Trigger a backup-to-cloud right now
curl -X POST \
     -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{}' \
     http://pawcorder.local:8080/api/backup/run-now

# Pause recording (privacy mode)
curl -X POST \
     -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"enabled": true, "paused_now": true}' \
     http://pawcorder.local:8080/api/privacy
```

The Bearer token also bypasses the CSRF header — that's the whole point.

## Home Assistant — RESTful sensor

Drop this in your `configuration.yaml`. Replace `pawcorder.local` and
the bearer token.

```yaml
sensor:
  - platform: rest
    name: Pawcorder pets today
    resource: http://pawcorder.local:8080/api/pets/today
    headers:
      Authorization: !secret pawcorder_bearer
    json_attributes_path: "$"
    json_attributes:
      - moments
    value_template: "{{ value_json.moments | length }}"
    scan_interval: 60

rest_command:
  pawcorder_pause:
    url: http://pawcorder.local:8080/api/privacy
    method: POST
    headers:
      Authorization: !secret pawcorder_bearer
      Content-Type: application/json
    payload: '{"enabled": true, "paused_now": true}'

  pawcorder_resume:
    url: http://pawcorder.local:8080/api/privacy
    method: POST
    headers:
      Authorization: !secret pawcorder_bearer
      Content-Type: application/json
    payload: '{"enabled": true, "paused_now": false}'
```

Then in `secrets.yaml`:

```yaml
pawcorder_bearer: "Bearer pwc_AbCdEf..."
```

You now have a sensor counting today's pet sightings and two scripts
to pause/resume recording from any HA automation.

## iOS Shortcuts — quick "is the cat OK"

1. Shortcuts app → New shortcut → "Get Contents of URL"
2. URL: `http://pawcorder.local:8080/api/pets/today`
3. Method: GET
4. Headers: `Authorization` = `Bearer pwc_AbCdEf...`
5. Add a "Get Dictionary Value" → key path `moments.0.pet_name`
6. Show Result.

Run from Apple Watch or Lock Screen. Returns the most recent sighting's
pet name in 1 second.

## Webhooks (Frigate → admin)

Frigate already POSTs every event to `/api/frigate/event` via the
template's `review.webhooks` block. You don't normally need to touch
this — it's the path for sub-second push to Telegram / LINE / WebPush.

If you have a third-party integration that wants the same data:

- the request body matches Frigate's
  [event webhook payload](https://docs.frigate.video/integrations/webhooks)
  shape: `{"type": "new" | "update" | "end", "after": {...event...}}`
- pawcorder's endpoint is auth-free for same-host trust; if you expose
  it externally, gate it via reverse proxy.

## Webhook from pawcorder to YOUR webhook

Want pawcorder events to fan out into something else (Slack, Discord,
n8n)? Two options:

1. **Telegram bridge:** if you use Telegram, every Slack/Discord/n8n
   bot can subscribe to your bot's chat. Path of least resistance.
2. **Outbound webhook (TODO):** there's no built-in outbound webhook
   yet — open a feature request. In the meantime, poll
   `/api/pets/today` from your tool.

## Available endpoints

A non-exhaustive list of endpoints useful from outside pawcorder:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/status` | GET | Cameras + Frigate state |
| `/api/pets` | GET | Pet list with today's stats |
| `/api/pets/today` | GET | Top recent moments (with snapshot URLs) |
| `/api/pets/health` | GET | Per-pet last-seen + anomaly flags |
| `/api/pets/correlation` | GET | Pairwise time-together |
| `/api/system/health` | GET | Storage / Frigate / camera status |
| `/api/system/perf` | GET | CPU / RAM / network per container |
| `/api/system/bandwidth` | GET | Per-camera bandwidth estimate |
| `/api/highlights` | GET | List daily reels |
| `/api/timelapse` | GET | List daily time-lapses |
| `/api/cameras/<n>/thumbnail` | GET | latest.jpg proxied to admin port |
| `/api/cameras/<n>/heatmap` | GET | 30-day activity heatmap PNG |
| `/api/cameras/<n>/ptz` | POST | move/preset/stop/zoom |
| `/api/privacy` | POST | Toggle privacy mode |
| `/api/backup/run-now` | POST | Manual backup-to-cloud now |
| `/api/energy-mode` | POST | Update quiet-hour schedules |
| `/api/pets/reenroll` | POST | Re-embed every reference photo against the active backbone (after switching MobileNet → DINOv2 or vice versa). Synchronous; returns counts. |
| `/api/recognition/stats` | GET | Recognition diagnostics: per-pet score histograms, multi-frame coverage, cloud-boost status, confidence mix over the last 14 days. |
| `/api/system/ai-tokens` | GET / POST | Admin AI provider config: BYOK keys for OpenAI / Gemini / Anthropic / Ollama, LLM and TTS provider preference, embedding backbone, conformal anomaly sensitivity (0.01-0.30, default 0.10). POST writes the values to `.env` and refreshes the in-process state — no restart required for backbone, provider, or sensitivity swaps. |
| `/api/pets/<id>/train-cloud/upload` | POST | Multipart upload of 20-60 reference photos for the per-pet custom model. Pro-only. |
| `/api/pets/<id>/train-cloud/status` | GET | Latest job state for one pet. |
| `/api/pets/<id>/train-cloud/forget` | POST | Owner-triggered purge of uploaded photos + trained model on the relay. |

Everything returns JSON. Errors return `{"error": "..."}` with a
4xx / 5xx status.

## Audio detection labels

When a camera's `audio_detection` is enabled, Frigate fires events for:

- `bark` — dog barking
- `meow` — cat meowing
- `yell` — human yelling
- `scream` — high-pitched scream
- `glass_break` — glass breaking (security)
- `smoke_alarm` — smoke alarm beep

These show up in `/api/events` like any other Frigate label and flow
through the same notification pipeline (Telegram / LINE / WebPush).
A "bark" event in `/api/pets/today` next to your dog's snapshot
becomes a free "doorbell" feature.

Frigate version note: audio detection ships with Frigate 0.13+. Older
versions silently ignore the audio block.
