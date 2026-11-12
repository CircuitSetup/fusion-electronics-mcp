"""Canonical representations used in compiler and artifact identities."""

from collections.abc import Mapping
from hashlib import sha256
import json
import re
from sys import float_info
import xml.etree.ElementTree as ET


def _json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_canonical"):
        return value.to_canonical()
    raise TypeError(f"{type(value).__name__} is not a canonical JSON value")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=_json_default,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def normalize_mm(value: int | float) -> float:
    if type(value) not in (int, float) or not -float_info.max <= value <= float_info.max:
        raise ValueError("millimetres must be a finite number")
    return round(float(value), 4) or 0.0


def eagle_xml_bytes(root: ET.Element, template_xml: bytes) -> bytes:
    """Serialize UTF-8 EAGLE XML with the pinned template's original prolog."""
    prolog = re.match(
        rb"(?:\xef\xbb\xbf)?(?:\s+|<\?.*?\?>|<!--.*?-->|<!DOCTYPE\s+eagle\b[^>]*>)*",
        template_xml, flags=re.DOTALL,
    )[0]
    body = ET.tostring(root, encoding="utf-8").replace(b"\r", b"&#13;")
    return prolog + body + b"\n"
