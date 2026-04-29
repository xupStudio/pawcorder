# Contributing to pawcorder

Thanks for your interest. pawcorder is MIT-licensed; contributions
keep that license. By submitting a PR, you agree to release your work
under MIT.

## Development setup

Linux, macOS, or Windows with WSL2:

```sh
git clone https://github.com/xupStudio/pawcorder.git
cd pawcorder

# Python admin panel
cd admin
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Run the panel with mock data (no Docker daemon needed):
python -m app.demo
# → http://localhost:8080  password: demo
```

## Tests

```sh
make test          # pytest + shellcheck
make test-py       # just Python
make test-shell    # just shellcheck (needs `brew install shellcheck` on macOS)
```

CI runs the same on every PR. **All tests must pass before merge.**

## House style

- **Python:** PEP 8, 4 spaces, type hints on public functions. New
  modules should have a 1-paragraph docstring at the top explaining
  the module's responsibility.
- **HTML / Jinja:** keep markup grouped logically; user-visible
  strings go through `t('KEY')` so translations don't break.
- **Bash:** target `bash 4+`, `set -euo pipefail`, run shellcheck.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/)
  style — `feat:`, `fix:`, `test:`, `docs:`, etc. Keep each commit
  focused on one logical change. Message body explains the *why*
  more than the *what*.

## Adding a translation

1. Add the new English string to `admin/app/i18n.py` under a new key.
2. Add the matching `zh-TW` translation in the same dict.
3. Use `{{ t('YOUR_KEY') }}` in Jinja templates or `t('YOUR_KEY', lang)`
   in Python.
4. The test `test_all_keys_have_zh_tw` enforces both languages exist.

## Adding a Frigate detector

1. In `admin/app/platform_detect.py`, add an entry to `VALID_DETECTORS`
   and a branch in `recommended_detector` that picks it.
2. In `config/frigate.template.yml`, add a `{% elif detector_type == 'X' %}`
   branch that emits the right Frigate config for it.
3. If the detector needs a Docker device or runtime override, add a
   `docker-compose.linux-X.yml` and update `lib.sh::detect_platform` to
   chain it via `RECOMMENDED_COMPOSE_FILES`.
4. Add a test in `tests/test_platform_detect.py`.

## Adding a cloud backend

1. Add the rclone backend name to `admin/app/cloud.py::SUPPORTED_BACKENDS`.
2. Add a branch in `fields_for_backend` that whitelists which fields
   we accept (we don't pass arbitrary keys into rclone.conf).
3. Add a section in `admin/app/templates/cloud.html` so the modal
   shows the right form fields for the new backend.
4. Add a row in the `test_fields_for_backend_filters_unknown` parametrize
   in `tests/test_cloud.py`.

## Reporting a bug

Useful info to include:
- Your platform: `uname -a`, distro, Docker version
- The detected detector: `docker compose exec admin python -c "from app.platform_detect import detect; print(detect().to_dict())"` (or just paste the /hardware page)
- Frigate logs: `make frigate-logs` last 50 lines
- Whether `make test` passes locally

## Security

If you find a security issue (auth bypass, code execution, secret
leak), please email the maintainers privately rather than filing a
public issue. We'll triage within a week.
