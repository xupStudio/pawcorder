# Security policy

Pawcorder is a self-hosted pet camera NVR. The threat model assumes
the operator trusts their own LAN; the security boundary is the
public-internet-facing path (Tailscale / Connect / port-forward).
Every report below is read by the maintainers within 72 hours.

## Reporting a vulnerability

**Please do not file a public GitHub issue for security bugs.**

Email `security@pawcorder.app` with:

- A description of the issue (what an attacker can do).
- The minimum reproduction steps you have.
- Your preferred contact channel (email is fine).
- Optionally, a CVSSv4 vector if you have one.

You will get an acknowledgement within 72 hours. We aim to ship a fix
within 14 days for critical issues (RCE, auth bypass, data exposure)
and 30 days for everything else. Coordinated disclosure timing is
negotiable; the default is "publish 90 days after the fix ships."

GPG: if you'd rather encrypt the report, the maintainer's key is
available at <https://keys.openpgp.org/search?q=security@pawcorder.app>.

## What's in scope

- The admin panel (`admin/app/*`) — auth, route handlers, template
  rendering.
- The relay (`relay/*`, Pro repo only) — license validation,
  per-tenant cloud-train encryption, OpenAI / Stripe webhook proxies.
- The Frigate config rendering pipeline (`config/frigate.template.yml`).
- Default Docker / docker-compose configurations the installer
  generates.
- The marketing site (`marketing/index.html`) — XSS, mixed-content,
  embed surfaces.

## Out of scope

- Vulnerabilities in upstream Frigate, Docker, OpenAI, etc. (file
  those with the upstream project).
- Issues that require local access to the Pawcorder host (e.g. an
  attacker who already has shell on the box). Pawcorder is a
  self-hosted appliance — local-root threats are explicitly outside
  the model.
- DoS via overwhelming a single endpoint with traffic if the host
  doesn't have a reverse proxy in front. Reverse-proxy rate-limiting
  is the operator's responsibility.

## Hardening posture (for operators)

A few defaults Pawcorder ships with that you can verify:

- **Admin password** is hashed with `bcrypt` at rest. Sessions use a
  signed cookie (`ADMIN_SESSION_SECRET` is auto-generated; rotating
  invalidates every session).
- **CSRF** — all `POST` / `PUT` / `DELETE` routes require an
  `X-Requested-With: pawcorder` header, set by the admin's own
  fetch helpers. Bearer-token API keys bypass CSRF on purpose.
- **API keys** are stored as SHA-256 hashes; the plaintext is shown
  exactly once at creation.
- **Cloud-train (Pro)** photos are encrypted at rest with per-tenant
  Fernet keys derived via HKDF from the relay's master secret.
  Photos auto-purge 30 days after upload, success path or failure.
- **Recognition cloud-model files** carry a magic prefix + length-
  prefixed pickle envelope; deserialise rejects payloads larger than
  1 MiB and enforces a backbone-name match before pickle.loads runs.

## Known security limitations

- The license verification secret on the relay (`PAWCORDER_LICENSE_SECRET`)
  is single-tenant. Rotating invalidates every issued license at once.
  v2 will move to a JWK-style key set with non-disruptive rotation.
- The cloud-train kernel uses `pickle` for serialising the trained
  classifier. The deserialise path is gated on magic + size + backbone
  string, but pickle has a long history of CVEs — if you don't
  trust the relay, don't enable cloud-train.
- The Pawcorder admin embeds external resources at runtime: Tailwind
  CDN (no SRI on purpose because the URL serves a JIT bundle). The
  font files are now self-hosted (since Batch 4) but Tailwind itself
  is still a CDN dep. SRI / self-hosting Tailwind is on the roadmap
  in `docs/HUMAN_WORK.md`.
