"""Encrypted store for the user's home Wi-Fi password.

We use AES-256-GCM with a per-record random nonce. The 32-byte key comes
from ``master_key.get_master_key()`` — the master-key module decides
where it lives (TPM / OS keyring / file), and this module focuses on the
record format.

Record format (JSON, one file per saved Wi-Fi profile, on disk under
``$PAWCORDER_DATA_DIR/wifi/``)::

    {
      "ssid":       "<plaintext SSID>",
      "auth":       "wpa2-psk" | "wpa3-sae" | "open" | "...",
      "nonce":      "<base64 12 bytes>",
      "ciphertext": "<base64 PSK ciphertext + GCM tag>",
      "saved_at":   "2026-05-02T18:23:11Z"
    }

SSID is *not* encrypted — knowing the SSID isn't sensitive on its own
(neighbours can see it broadcast), and we want to render the saved-network
list in the admin UI without unsealing the master key on every page load.
The PSK is the only secret bit and lives only inside ``ciphertext``.

We deliberately keep this module dependency-light: it pulls
``cryptography`` (already a transitive of ``pywebpush``) and stdlib only.
The file-per-record layout means a corrupted record only loses one
network, not the whole vault.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import master_key

logger = logging.getLogger("pawcorder.wifi_creds")

DATA_DIR = Path(os.environ.get("PAWCORDER_DATA_DIR", "/data"))
WIFI_DIR = DATA_DIR / "wifi"

# AES-GCM nonce — RFC 5116 recommends 96 bits.
_NONCE_LENGTH = 12

# Allowed auth strings. Cameras' provisioning APIs care about this — a
# Tapo will reject "wpa2-psk" if the home AP is actually wpa3-sae. We
# don't enforce here, only normalise.
_AUTH_VALUES = ("open", "wpa2-psk", "wpa3-sae", "wpa3-personal", "wpa-eap")

# Filenames are derived from the SSID. We tolerate any UTF-8 SSID by
# hex-encoding bytes that aren't safe for typical filesystems. Pure ASCII
# alphanumeric SSIDs round-trip without encoding so admins can still grep
# the directory.
_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9._-]")


class WifiCredsError(RuntimeError):
    """Raised on decrypt / parse failures.

    Catching ``WifiCredsError`` separately from generic ``RuntimeError``
    lets the admin route distinguish "user clicked save with a typo'd
    SSID" from "the master key changed and the vault is now unreadable".
    """


@dataclass
class WifiCredential:
    """A saved Wi-Fi profile. ``psk`` is plaintext after decrypt."""
    ssid: str
    psk: str
    auth: str
    saved_at: str  # ISO-8601 UTC, "...Z"

    def to_safe_dict(self) -> dict:
        """JSON shape suitable for the admin UI — *no* PSK in it.

        Calling code that needs the PSK must use the unsealed
        ``WifiCredential`` directly. The admin page should never need to
        echo a saved password back to the browser, only its presence.
        """
        return {
            "ssid": self.ssid,
            "auth": self.auth,
            "saved_at": self.saved_at,
            "has_password": bool(self.psk),
        }


def _ssid_to_filename(ssid: str) -> str:
    """Map an SSID to a flat-filesystem filename.

    SSIDs can contain almost any byte (the 802.11 standard caps the field
    at 32 bytes, no charset constraint). We percent-style-encode anything
    that isn't a safe filesystem character so:

      - Two distinct SSIDs never collide on disk
      - The user can still see the SSID by inspecting the file basename
        when it's pure ASCII (the common case)

    The encoding is reversible only for our own purposes via the JSON
    body, not from the filename — that's intentional.
    """
    out: list[str] = []
    for ch in ssid:
        b = ch.encode("utf-8")
        if len(b) == 1 and _SAFE_FILENAME_RE.match(ch):
            out.append(ch)
        else:
            out.append("".join(f"_{by:02x}" for by in b))
    if not out:
        # Empty SSID — never legal but defend against it anyway.
        out.append("_empty")
    return "".join(out) + ".json"


def _record_path(ssid: str) -> Path:
    return WIFI_DIR / _ssid_to_filename(ssid)


def _aesgcm() -> AESGCM:
    info = master_key.get_master_key()
    return AESGCM(info.key)


def _now_iso_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalise_auth(auth: str) -> str:
    a = (auth or "").strip().lower()
    if a in _AUTH_VALUES:
        return a
    # Common aliases users / cameras pass.
    if a in ("wpa2", "psk", "wpa-psk", "wpa2psk"):
        return "wpa2-psk"
    if a in ("wpa3", "sae"):
        return "wpa3-sae"
    if a in ("nopass", "none", ""):
        return "open"
    # Pass-through unknown so a niche enterprise auth still saves; cams
    # that need an exact match will reject during provisioning, which is
    # a much clearer error than a silent rewrite here.
    return a


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save(ssid: str, psk: str, auth: str = "wpa2-psk") -> WifiCredential:
    """Encrypt & persist. Overwrites any prior record for the same SSID."""
    ssid = (ssid or "").strip()
    if not ssid:
        raise WifiCredsError("ssid is required")
    if len(ssid.encode("utf-8")) > 32:
        # 802.11 limit. Cameras choke on longer values anyway.
        raise WifiCredsError("ssid is longer than the 32-byte 802.11 limit")
    auth = _normalise_auth(auth)
    if auth != "open" and not psk:
        raise WifiCredsError(f"auth {auth!r} requires a password")

    WIFI_DIR.mkdir(parents=True, exist_ok=True)
    nonce = os.urandom(_NONCE_LENGTH)
    # Bind the SSID into the GCM associated-data so a stolen ciphertext
    # blob cannot be replayed under a different SSID's record.
    ad = ssid.encode("utf-8")
    ct = _aesgcm().encrypt(nonce, psk.encode("utf-8"), ad)

    record = {
        "ssid": ssid,
        "auth": auth,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ct).decode("ascii"),
        "saved_at": _now_iso_utc(),
    }
    p = _record_path(ssid)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return WifiCredential(ssid=ssid, psk=psk, auth=auth, saved_at=record["saved_at"])


def load(ssid: str) -> WifiCredential:
    """Decrypt one record. Raises ``WifiCredsError`` if missing / corrupt."""
    p = _record_path(ssid)
    if not p.exists():
        raise WifiCredsError(f"no saved Wi-Fi profile for SSID {ssid!r}")
    try:
        record = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WifiCredsError(f"could not read {p.name}: {exc}") from exc
    if record.get("ssid") != ssid:
        # Filename collision or hand-edited record — defend.
        raise WifiCredsError(
            f"file {p.name} ssid {record.get('ssid')!r} does not match request {ssid!r}"
        )
    nonce = base64.b64decode(record["nonce"])
    ct = base64.b64decode(record["ciphertext"])
    try:
        psk = _aesgcm().decrypt(nonce, ct, ssid.encode("utf-8")).decode("utf-8")
    except InvalidTag as exc:
        raise WifiCredsError(
            "Wi-Fi record decrypt failed — master key likely changed since save"
        ) from exc
    return WifiCredential(
        ssid=ssid,
        psk=psk,
        auth=record.get("auth", "wpa2-psk"),
        saved_at=record.get("saved_at", ""),
    )


def list_saved() -> list[WifiCredential]:
    """List saved profiles *without* decrypting PSKs.

    Lets the admin UI render the saved-network list cheaply and without
    risking accidental PSK exposure in a logging side-effect. Each entry
    has ``psk=""`` — call ``load()`` only at the moment a provisioner
    actually needs the password.
    """
    if not WIFI_DIR.exists():
        return []
    out: list[WifiCredential] = []
    for f in sorted(WIFI_DIR.iterdir()):
        if f.suffix != ".json":
            continue
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("skipping unreadable wifi record %s", f.name)
            continue
        ssid = record.get("ssid")
        if not isinstance(ssid, str):
            continue
        out.append(
            WifiCredential(
                ssid=ssid,
                psk="",
                auth=record.get("auth", "wpa2-psk"),
                saved_at=record.get("saved_at", ""),
            )
        )
    return out


def delete(ssid: str) -> bool:
    """Remove a saved profile. Returns True if a record was deleted."""
    p = _record_path(ssid)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
