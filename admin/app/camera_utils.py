"""Cross-vendor helpers shared by the camera auto-config modules.

Five of the six vendor helpers (Reolink, Hikvision, Dahua/Amcrest, Axis,
Foscam) ship the same three slabs of boilerplate:

  * URL-encoded RTSP URL builders that all do the same template-fill;
  * tiny XML helpers (``_localname`` / ``_find_text``) duplicated across
    Hikvision, Foscam, and the ONVIF helper;
  * a ``key=value\\n`` parser used by Dahua's CGI and Axis param.cgi
    (``Encode[0].MainFormat[0].Video.enabled=true`` style);
  * an identical try/except ladder around httpx errors that maps 401/403
    to ``PermissionError`` and timeouts/connect-errors to a
    "Could not reach <vendor>" ``RuntimeError``.

This module is the single home for all four. Vendor modules import from
here instead of carrying their own copies. Only stdlib + httpx, so the
licence footprint stays MIT/BSD/Apache.
"""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

import httpx


# --- XML helpers ----------------------------------------------------------

def xml_localname(tag: str) -> str:
    """Strip an XML namespace prefix: ``{ns}foo`` -> ``foo``.

    Hikvision XML lives under ``http://www.hikvision.com/ver20/XMLSchema``
    (occasionally ver10, or no namespace at all on older firmware). Foscam
    serves un-namespaced XML. Matching by local-name lets the same code
    handle every variant without per-firmware branches.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def xml_find_text(root: ET.Element, *names: str, default: str = "") -> str:
    """Return text of the first element whose local-name is in ``names``.

    Walks ``root.iter()`` and returns ``el.text.strip()`` for the first
    match. Returns ``default`` when no element has the given local-name or
    when the matching element has no text. Multiple names are useful when
    the same logical field appears under different tags across firmware
    versions (e.g. ``serialNumber`` vs ``SerialNumber``).
    """
    for el in root.iter():
        if xml_localname(el.tag) in names and el.text is not None:
            text = el.text.strip()
            if text:
                return text
    return default


# --- key=value text parser ------------------------------------------------

def parse_kv_text(text: str) -> dict[str, str]:
    """Parse ``key=value\\n`` CGI text into a flat dict.

    Used by Dahua/Amcrest (``Encode[0].MainFormat[0].Video.enabled=true``)
    and Axis param.cgi (``root.Brand.ProdNbr=M1054``). We do NOT try to
    rebuild a tree from the dotted/bracketed paths — for our purposes the
    leaf path *is* the key. Blank lines and lines without ``=`` are
    skipped. Both ``\\r\\n`` and bare ``\\n`` line endings are handled
    because the two CGIs disagree.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# --- httpx error mapping --------------------------------------------------

class VendorHttpErrors:
    """Async context manager mapping httpx errors to the canonical pair.

    Inside the ``async with`` block:

      * ``httpx.HTTPStatusError`` with status 401/403 ->
        ``PermissionError(f"{vendor} auth rejected for {ip}")``
      * any other ``httpx.HTTPStatusError`` ->
        ``RuntimeError(f"{vendor} HTTP {status} from {ip}")``
      * ``TimeoutException`` / ``ConnectError`` / ``NetworkError`` ->
        ``RuntimeError(f"Could not reach {vendor} camera at {ip}: {exc}")``
      * ``xml.etree.ElementTree.ParseError`` ->
        ``RuntimeError(f"{vendor} returned malformed XML from {ip}: {exc}")``
        — vendor firmware occasionally emits truncated or non-XML bodies;
        without this map the route handler shows a stack-trace string.

    ``PermissionError`` raised inside the block (e.g. by Foscam's CGI
    ``<result>1</result>`` translation) is re-raised unchanged.

    Usage::

        async with VendorHttpErrors("Hikvision", ip):
            async with httpx.AsyncClient(...) as client:
                ...
    """

    __slots__ = ("vendor", "ip")

    def __init__(self, vendor: str, ip: str) -> None:
        self.vendor = vendor
        self.ip = ip

    async def __aenter__(self) -> "VendorHttpErrors":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is None:
            return False
        # PermissionError already raised inside the block (e.g. by Foscam's
        # body-level <result>1</result> translation) carries its own
        # message — leave it alone.
        if isinstance(exc, PermissionError):
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in (401, 403):
                raise PermissionError(
                    f"{self.vendor} auth rejected for {self.ip}",
                ) from exc
            raise RuntimeError(
                f"{self.vendor} HTTP {status} from {self.ip}",
            ) from exc
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
            raise RuntimeError(
                f"Could not reach {self.vendor} camera at {self.ip}: {exc}",
            ) from exc
        if isinstance(exc, ET.ParseError):
            raise RuntimeError(
                f"{self.vendor} returned malformed XML from {self.ip}: {exc}",
            ) from exc
        # Anything else propagates untouched — surfacing unexpected
        # exception types is more useful than masking them.
        return False
