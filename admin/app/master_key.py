"""Platform-aware master-key storage for the Wi-Fi credential vault.

Pawcorder needs to remember the user's home Wi-Fi password long enough to
re-use it when onboarding additional cameras (and to retry a half-finished
provisioning later). We never store the password in plaintext on disk —
``wifi_creds`` encrypts it with AES-GCM. This module produces and protects
the AES key (the "master key") used by ``wifi_creds``.

The trade-off the master-key location decides is:

  * **TPM-sealed** (Linux + TPM 2.0): the key never leaves the chip in
    plaintext. Cloning the disk to a different host yields a key that
    cannot be unsealed. Best resistance to cold-storage theft.
  * **OS keyring** (macOS Keychain / Windows DPAPI / Linux Secret Service):
    relies on the OS user-session credential, which is unlocked at login.
    Headless installs need a configured Secret Service (gnome-keyring with
    PAM, or kwallet) — otherwise this backend is unavailable.
  * **File fallback**: 32 random bytes in ``$PAWCORDER_DATA_DIR/master.key``
    with mode 0600. Convenience first, security second — disk theft = key
    leak. Used only when the two stronger backends are unreachable.

Selection happens once at first call to ``get_master_key()`` and is
recorded in ``$PAWCORDER_DATA_DIR/master_key.meta.json`` so subsequent
runs use the same backend deterministically. Re-running the auto-detect
silently when the chosen backend is unavailable would lock users out of
their previously-saved Wi-Fi creds — we surface that error instead.

The module is import-light by design. ``tpm2_pytss`` and ``keyring`` are
imported lazily inside their respective probe functions so the admin
process never pays their import cost on hosts that don't need them.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger("pawcorder.master_key")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))

# 32 bytes = AES-256 key material. ``wifi_creds`` derives session keys
# from it via HKDF; we don't reuse the raw bytes for any other purpose.
KEY_LENGTH_BYTES = 32

_META_FILENAME = "master_key.meta.json"
_FILE_BACKEND_FILENAME = "master.key"
_KEYRING_SERVICE = "pawcorder"
_KEYRING_USERNAME = "master_key"

# TPM 2.0 persistent handle range is 0x81000000 – 0x81FFFFFF; we pick a
# value low enough to avoid clashing with vendor-reserved handles (the
# bottom of the range is conventionally user-space).
_TPM_PERSISTENT_HANDLE = 0x81015A77

BackendName = str  # "tpm" | "keyring" | "file"


@dataclass(frozen=True)
class MasterKeyInfo:
    """What ``get_master_key()`` reports about the active backend.

    Kept as a separate dataclass (rather than returning a tuple) so the
    admin UI can show *why* the user got the protection level they got
    without coupling to the bytes themselves.
    """
    key: bytes
    backend: BackendName
    detail: str


# ---------------------------------------------------------------------------
# Meta file
# ---------------------------------------------------------------------------


def _has_encrypted_data() -> bool:
    """True iff this admin has ever written an encrypted blob.

    We check the wifi-creds directory because that's the only consumer
    of the master key today; if it's missing or empty, swapping backends
    can't lose user data. The check tolerates the cred-store root
    moving (e.g. a different STORAGE_PATH) by reading its actual path
    from wifi_creds at call time.
    """
    try:
        from . import wifi_creds  # local import — circular at module top
    except ImportError:
        return False
    wifi_dir = getattr(wifi_creds, "WIFI_DIR", None)
    if wifi_dir is None or not wifi_dir.exists():
        return False
    # Any *.json record means we've successfully encrypted at least once.
    return any(wifi_dir.glob("*.json"))


def _meta_path() -> Path:
    return DATA_DIR / _META_FILENAME


def _read_meta() -> dict:
    p = _meta_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # If meta is corrupt we'd rather rebuild than crash — the worst
        # case is a re-pick of backend, which only matters if a saved
        # ciphertext exists from a previous run, and ``wifi_creds`` will
        # surface that mismatch with a clear error.
        logger.warning("master_key.meta.json corrupt — ignoring")
        return {}


def _write_meta(payload: dict) -> None:
    p = _meta_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# TPM backend (Linux + tpm2-pytss)
# ---------------------------------------------------------------------------


def _tpm_resource_present() -> bool:
    # /dev/tpmrm0 is the resource-managed TPM device the kernel exposes
    # once a TPM 2.0 chip is detected and the tpm_crb / tpm_tis driver
    # binds to it. /dev/tpm0 is the raw device; we prefer the RM one
    # because it serializes concurrent access — multiple Pawcorder
    # subprocesses using the TPM at once would otherwise step on each
    # other's session handles.
    return Path("/dev/tpmrm0").exists() or Path("/dev/tpm0").exists()


def _tpm_available() -> bool:
    if not _tpm_resource_present():
        return False
    try:
        import tpm2_pytss  # noqa: F401  - import probe only
    except Exception:  # noqa: BLE001 — any import failure means "not usable"
        return False
    return True


def _tpm_get_or_create_key() -> bytes:
    """Read the persistent-handle sealed key, or seal a fresh one.

    The key is sealed under the storage hierarchy with no PCR policy, so
    it survives reboots but is bound to *this* TPM chip — exactly the
    property we want for "disk theft can't unlock saved Wi-Fi creds".
    """
    from tpm2_pytss import ESAPI, TPM2_RH, TPM2B_SENSITIVE_CREATE  # type: ignore[import]
    from tpm2_pytss.types import (  # type: ignore[import]
        TPMS_SENSITIVE_CREATE,
        TPM2B_PUBLIC,
        TPM2B_DATA,
        TPMS_PCR_SELECTION,
        TPML_PCR_SELECTION,
    )

    with ESAPI() as esapi:
        # Walk the persistent handles to find ours. tpm2-pytss returns a
        # TPMS_CAPABILITY_DATA with handle list; we do an existence check
        # rather than blindly Load because EvictControl semantics differ
        # across tpm2-pytss minor versions.
        existing = _tpm_persistent_handle_present(esapi, _TPM_PERSISTENT_HANDLE)
        if existing:
            data = esapi.unseal(existing)
            # ``unseal`` returns a TPM2B_SENSITIVE_DATA; .buffer is bytes.
            return bytes(data.buffer)

        # Seal a fresh 32-byte secret under the storage primary.
        primary = esapi.create_primary(
            in_sensitive=TPM2B_SENSITIVE_CREATE(),
            primary_handle=TPM2_RH.OWNER,
        )
        secret = secrets.token_bytes(KEY_LENGTH_BYTES)

        sensitive = TPMS_SENSITIVE_CREATE()
        sensitive.data.buffer = secret

        sealed = esapi.create(
            parent_handle=primary.handle,
            in_sensitive=TPM2B_SENSITIVE_CREATE(sensitive=sensitive),
            in_public=_tpm_keyed_hash_template(),
            outside_info=TPM2B_DATA(),
            creation_pcr=TPML_PCR_SELECTION(),
        )
        loaded = esapi.load(primary.handle, sealed.private, sealed.public)

        # EvictControl into the persistent handle range so the seal
        # survives reboot and the TCTI being torn down.
        esapi.evict_control(
            auth=TPM2_RH.OWNER,
            object_handle=loaded,
            persistent_handle=_TPM_PERSISTENT_HANDLE,
        )
        esapi.flush_context(loaded)
        esapi.flush_context(primary.handle)
        return secret


def _tpm_persistent_handle_present(esapi, handle: int) -> int | None:
    """Return the ESYS handle if ``handle`` exists persistently, else None."""
    from tpm2_pytss.constants import TPM2_CAP, TPM2_HC  # type: ignore[import]

    more, caps = esapi.get_capability(
        TPM2_CAP.HANDLES,
        TPM2_HC.PERSISTENT_FIRST,
        TPM2_HC.PERSISTENT_LAST - TPM2_HC.PERSISTENT_FIRST,
    )
    handles = caps.data.handles.handle
    if handle in handles:
        # tr_from_tpmpublic gives us an ESYS handle suitable for ``unseal``.
        return esapi.tr_from_tpmpublic(handle)
    return None


def _tpm_keyed_hash_template():
    """A keyed-hash sealed-data object template (no PCR / password policy)."""
    from tpm2_pytss.types import TPM2B_PUBLIC, TPMT_PUBLIC  # type: ignore[import]
    from tpm2_pytss.constants import (  # type: ignore[import]
        TPM2_ALG,
        TPMA_OBJECT,
    )

    pub = TPMT_PUBLIC()
    pub.type = TPM2_ALG.KEYEDHASH
    pub.name_alg = TPM2_ALG.SHA256
    pub.object_attributes = (
        TPMA_OBJECT.FIXEDTPM
        | TPMA_OBJECT.FIXEDPARENT
        | TPMA_OBJECT.NODA
    )
    pub.parameters.keyedhash_detail.scheme.scheme = TPM2_ALG.NULL
    return TPM2B_PUBLIC(public_area=pub)


# ---------------------------------------------------------------------------
# OS keyring backend
# ---------------------------------------------------------------------------


def _keyring_available() -> bool:
    try:
        import keyring  # type: ignore[import]
        from keyring.errors import KeyringError  # type: ignore[import]  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    try:
        # keyring.get_keyring() probes the platform backends and raises
        # if there's literally no usable backend (e.g. a Linux server with
        # no Secret Service). On macOS it returns the Keychain backend
        # without prompting for unlock.
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return False
    # ``keyring.backends.fail.Keyring`` and ``keyring.backends.null.Keyring``
    # are what the library returns when no real backend is available.
    # The class itself is just ``Keyring`` so checking ``__name__`` alone
    # always missed the fail backend — we have to look at the module path
    # too. Without this, a containerised admin with no Secret Service /
    # Keychain access lands on the no-op backend and every save() raises
    # ``NoKeyringError`` instead of falling through to the file backend.
    cls = type(backend)
    name = cls.__name__.lower()
    module = (cls.__module__ or "").lower()
    if "fail" in name or "null" in name or "fail" in module or "null" in module:
        return False
    return True


def _keyring_get_or_create_key() -> bytes:
    import keyring  # type: ignore[import]

    existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    if existing:
        # We store the key as hex so the value round-trips through the
        # keyring's mandatory-string interface without binary encoding
        # surprises (some Linux Secret Service implementations reject
        # non-UTF-8 bytes).
        return bytes.fromhex(existing)
    secret = secrets.token_bytes(KEY_LENGTH_BYTES)
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, secret.hex())
    return secret


# ---------------------------------------------------------------------------
# File backend
# ---------------------------------------------------------------------------


def _file_path() -> Path:
    return DATA_DIR / _FILE_BACKEND_FILENAME


def _file_get_or_create_key() -> bytes:
    p = _file_path()
    if p.exists():
        data = p.read_bytes()
        if len(data) != KEY_LENGTH_BYTES:
            raise RuntimeError(
                f"master.key has wrong length ({len(data)} != {KEY_LENGTH_BYTES}) — "
                "refusing to use a corrupt key file"
            )
        return data
    p.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(KEY_LENGTH_BYTES)
    # Open with O_CREAT|O_EXCL|O_WRONLY and mode 0600 so we never even
    # briefly land a world-readable temp on disk. ``Path.write_bytes``
    # would race here.
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    return secret


# ---------------------------------------------------------------------------
# Backend registry + auto-pick
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BackendSpec:
    name: BackendName
    detail: str
    available: Callable[[], bool]
    get_or_create: Callable[[], bytes]


_BACKENDS: tuple[_BackendSpec, ...] = (
    _BackendSpec(
        name="tpm",
        detail="TPM 2.0 sealed (host-bound, disk theft cannot decrypt)",
        available=_tpm_available,
        get_or_create=_tpm_get_or_create_key,
    ),
    _BackendSpec(
        name="keyring",
        detail="OS keyring (Keychain / DPAPI / Secret Service)",
        available=_keyring_available,
        get_or_create=_keyring_get_or_create_key,
    ),
    _BackendSpec(
        name="file",
        detail="encrypted file at $PAWCORDER_DATA_DIR/master.key (mode 0600)",
        available=lambda: True,
        get_or_create=_file_get_or_create_key,
    ),
)


def _pick_backend() -> _BackendSpec:
    """Choose the strongest available backend at first run.

    Caller of ``get_master_key()`` then persists the choice to
    ``master_key.meta.json`` so subsequent runs go straight to that
    backend.
    """
    for spec in _BACKENDS:
        try:
            if spec.available():
                return spec
        except Exception as exc:  # noqa: BLE001
            logger.warning("backend probe failed for %s: %s", spec.name, exc)
            continue
    # The file backend's `available` always returns True, so this branch
    # is unreachable in practice. Kept for static-analysis happiness.
    return _BACKENDS[-1]


def _spec_by_name(name: BackendName) -> _BackendSpec | None:
    for s in _BACKENDS:
        if s.name == name:
            return s
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_master_key(*, force_backend: BackendName | None = None) -> MasterKeyInfo:
    """Return the master key, picking + remembering a backend on first call.

    ``force_backend`` is an escape hatch for tests and for the migration
    path where an admin wants to deliberately move from one backend to
    another (e.g. a host got a TPM upgrade). It bypasses the persisted
    meta — callers should write fresh meta themselves after rotating.

    Raises ``RuntimeError`` if the persisted backend is no longer available
    (e.g. the user moved the host to a TPM-less machine after first picking
    TPM). We surface this rather than silently downgrading because a
    silent downgrade would invalidate every encrypted Wi-Fi credential the
    user had already saved.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if force_backend is not None:
        spec = _spec_by_name(force_backend)
        if spec is None:
            raise ValueError(f"unknown backend {force_backend!r}")
    else:
        meta = _read_meta()
        chosen = meta.get("backend")
        if chosen:
            spec = _spec_by_name(chosen)
            if spec is None:
                raise RuntimeError(
                    f"persisted master_key backend {chosen!r} is unknown — "
                    "delete master_key.meta.json to reset"
                )
            if not spec.available():
                # Self-heal when nothing has ever been encrypted with the
                # persisted backend — that's the post-bug state where an
                # earlier install incorrectly picked "keyring" inside a
                # Docker container (the no-op backend), every save crashed,
                # and no real ciphertexts exist to invalidate. Quietly
                # re-pick rather than wedge the user. We DON'T self-heal
                # when ciphertexts exist — that would silently destroy
                # the user's saved Wi-Fi credentials.
                if not _has_encrypted_data():
                    logger.warning(
                        "persisted master_key backend %r unavailable; "
                        "no encrypted data found, re-picking backend",
                        chosen,
                    )
                    spec = _pick_backend()
                    _write_meta({"backend": spec.name, "detail": spec.detail})
                else:
                    raise RuntimeError(
                        f"persisted master_key backend {chosen!r} is no longer "
                        "available on this host — restoring a backup or migrating "
                        "by hand is required to keep saved Wi-Fi creds readable"
                    )
        else:
            spec = _pick_backend()
            _write_meta({"backend": spec.name, "detail": spec.detail})

    key = spec.get_or_create()
    if len(key) != KEY_LENGTH_BYTES:
        raise RuntimeError(
            f"backend {spec.name!r} returned a key of unexpected length "
            f"({len(key)} != {KEY_LENGTH_BYTES})"
        )
    return MasterKeyInfo(key=key, backend=spec.name, detail=spec.detail)


def describe_active_backend() -> dict:
    """Cheap, no-secret-leaking summary for the admin UI."""
    meta = _read_meta()
    if not meta:
        return {"backend": "unconfigured", "detail": "no master key yet"}
    return {
        "backend": meta.get("backend", "unknown"),
        "detail": meta.get("detail", ""),
    }


def reset_for_tests() -> None:
    """Drop on-disk meta + file-backend key so a fresh test run can re-init.

    ``data_dir`` fixture in conftest.py wipes the whole tmpdir between
    tests, but tests that monkeypatch DATA_DIR mid-test need a way to
    clear the cached state without leaning on filesystem cleanup.
    """
    for p in (_meta_path(), _file_path()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
