# Pawcorder usability harness

Persona-driven Playwright walkthrough of the admin UI. Used to find UX
friction (Batch 7 — plain-language fixes) and engineer-work touchpoints
(Batch 8 — automation).

## Run a persona

```bash
cd admin && make demo &           # spin up admin at :8081 with mock data
cd _testdir/usability
npm install                        # one-time: pulls Playwright (~100 MB)
npx playwright install chromium    # one-time: pulls headless Chromium

node scripts/harness.mjs grandma   scripts/plans/grandma.json
node scripts/harness.mjs poweruser scripts/plans/poweruser.json
node scripts/harness.mjs hostile   scripts/plans/hostile.json
```

Each run drops:
- `screenshots/<persona>/NN-*.png` — full-page screenshots after every step
- `reports/<persona>-trace.md` — visible interactables + body excerpts per step

Both are gitignored — they're regenerated on every run.

## Plan format

Plans are JSON: a `locale` and an array of `steps`. Each step is one of:

```jsonc
{ "goto": "/path" }                                   // navigate
{ "click": "label", "role": "button", "exact": true } // click by accessible name
{ "click": "css=button.foo" }                         // click by CSS selector
{ "fill": "label-or-css", "value": "..." }            // type into a field
{ "press": "Enter" }                                  // keyboard
{ "wait": 1500 }                                      // ms
{ "note": "what the persona is thinking" }            // narrative line in trace
```

The harness uses an iPhone 13 viewport by default — most pet-owner users
land on Pawcorder via the LAN URL on their phone first.

## Personas committed

- `grandma.json` — zh-TW, 65, tech-illiterate. Tests plain-language copy.
- `poweruser.json` — en-US, fast skim-read. Tests EN locale + power flows.
- `hostile.json` — wrong passwords, dead URLs, fuzz inputs. Tests error pages.
- `sanity.json` — edge cases: login disclosure expanded, /404 paths.
- `mobile-cameras.json` — verifies live-view modal opens from dashboard tile.
- `batch8.json` / `batch8-edges.json` — verifies Batch 8 features render.

Add new personas by dropping a `plans/<name>.json` file — the harness picks
it up automatically.

## Curated docs (kept in repo)

- [`reports/REPORT.md`](reports/REPORT.md) — Batch 7 friction findings
- [`reports/AUTOMATION_PROPOSAL.md`](reports/AUTOMATION_PROPOSAL.md) — Batch 8 engineer-work elimination proposal
