"""Small pure helpers (no I/O), easy to unit-test."""

from __future__ import annotations

import re

# Meta rejects template parameters containing newlines/tabs or more than
# four consecutive spaces; we also cap the length to stay well under the
# 1024-character body limit.
MAX_PARAM_LEN = 900


class InvalidPhone(ValueError):
    pass


def normalize_phone(raw: str, default_country: str = "55") -> str:
    """Return the number as digits only, with country code (E.164 without '+').

    Brazilian numbers typed without country code (10 or 11 digits, i.e. DDD +
    number) get ``default_country`` prepended. A leading '+' or '00' means the
    country code is already present.
    """
    s = raw.strip()
    has_plus = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    if not has_plus and len(digits) in (10, 11) and default_country:
        digits = default_country + digits
    if not 10 <= len(digits) <= 15:
        raise InvalidPhone(f"Invalid phone number: {raw!r}")
    return digits


_PHONE_TOKEN = re.compile(r"^[\d()+\-.]+$")


def split_phone_args(args: list[str]) -> tuple[str, list[str]]:
    """Split command arguments into (phone, rest), allowing the phone number
    to be typed with spaces, e.g. ``(21) 99999-1234 formulario``."""
    i = 0
    while i < len(args) and _PHONE_TOKEN.match(args[i]):
        i += 1
    if i == 0:
        return (args[0] if args else ""), args[1:]
    return " ".join(args[:i]), args[i:]


def mask_phone(phone: str) -> str:
    """Hide the middle digits for logs and room notices (LGPD-friendly)."""
    if len(phone) <= 6:
        return "*" * len(phone)
    return phone[:4] + "*" * (len(phone) - 8) + phone[-4:]


def sanitize_param(text: str, max_len: int = MAX_PARAM_LEN) -> str:
    """Make arbitrary text acceptable as a template body parameter."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\s*\n+\s*", " | ", t)
    t = t.replace("\t", " ")
    t = re.sub(r" {2,}", " ", t).strip()
    if not t:
        t = "-"
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t
