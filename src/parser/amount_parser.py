"""Parsing utilities for Myanmar and English amounts, references, and formatting."""

from __future__ import annotations

import re

MY_DIGITS = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
# number, optional thousands separators, optional K/M suffix
AMOUNT_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?(?![\w.])"
)
# Reference numbers in a reply target: phone/account/order-like bare digit runs.
# Allows spaces/hyphens between digits, e.g. 09 672 376 152.
REFERENCE_RE = re.compile(r"(?<!\d)(?:\d[ \t-]*){4,11}\d(?!\d)")


def normalize_search(value: str) -> str:
    """Digits-only search key; 035 265 and 035265 become the same query."""
    if not value:
        return ""
    return "".join(MY_DIGITS.get(ord(ch), ch) for ch in value if ch.isdigit() or ch in "၀၁၂၃၄၅၆၇၈၉")


def normalize_phone_reference(ref: str | None) -> str | None:
    """Normalize Myanmar phone and reference numbers into standard 09... format."""
    if not ref:
        return None
    digits = normalize_search(ref)
    if not digits:
        return None
    # Country code: 959... -> 09...
    if digits.startswith("959") and len(digits) >= 10:
        return "09" + digits[3:]
    # Missing leading zero: 9xxxxxxxxx (9-10 digits) -> 09xxxxxxxxx
    if digits.startswith("9") and len(digits) in (9, 10):
        return "0" + digits
    return digits


def is_reference_match(ref1: str | None, ref2: str | None) -> bool:
    """Check if two reference/phone numbers refer to the same target."""
    if not ref1 or not ref2:
        return False
    norm1 = normalize_phone_reference(ref1) or ""
    norm2 = normalize_phone_reference(ref2) or ""
    if not norm1 or not norm2:
        return False
    if norm1 == norm2:
        return True
    # If both are long enough digit sequences (>= 7 digits), check suffix/substring match
    # e.g., '675362816' or '5362816' matching '09675362816'
    if len(norm1) >= 7 and len(norm2) >= 7:
        if norm1.endswith(norm2) or norm2.endswith(norm1) or norm1 in norm2 or norm2 in norm1:
            return True
    return False


def parse_amounts(text: str, min_bare: int = 1000, max_bare_digits: int = 6) -> list[int]:
    """Extract amounts from free text. Returns integer values."""
    if not text:
        return []
    cleaned = URL_RE.sub(" ", text).translate(MY_DIGITS)
    found: list[tuple[int, int]] = []  # (value, position)
    for m in AMOUNT_RE.finditer(cleaned):
        raw, suffix = m.group(1), m.group(2)
        grouped = "," in raw
        digits = raw.replace(",", "").split(".")[0]
        num = float(raw.replace(",", ""))
        if suffix in ("k", "K"):
            value = num * 1_000
        elif suffix in ("m", "M"):
            value = num * 1_000_000
        elif grouped:
            value = num  # "15,000" is explicitly money-formatted
        else:
            # Bare digits: reject phone/account numbers and leading-zero runs.
            if raw.startswith("0") or len(digits) > max_bare_digits or num < min_bare:
                continue
            value = num
        value = int(round(value))
        if value > 0:
            found.append((value, m.start()))

    # Drop a written-out grand total: "5K + 25K + 15K = 45K" must not double count.
    eq = max(cleaned.rfind("="), cleaned.rfind("＝"))
    if eq >= 0:
        before = [v for v, p in found if p < eq]
        after = [(v, p) for v, p in found if p > eq]
        if before and after and sum(v for v, _ in after) == sum(before):
            found = [(v, p) for v, p in found if p < eq]

    return [v for v, _ in found]


def fmt(n: int, suffix: str = "") -> str:
    """Format integer with thousands separator and optional currency suffix."""
    s = f"{n:,}"
    return f"{s} {suffix}".strip() if suffix else s


def label(n: int) -> str:
    """Denomination label: 5000 -> 5K, 1500 -> 1.5K, 2000000 -> 2M."""
    if n >= 1_000_000 and n % 100_000 == 0:
        m = n / 1_000_000
        return f"{int(m)}M" if m.is_integer() else f"{m:g}M"
    if n >= 1_000 and n % 100 == 0:
        k = n / 1_000
        return f"{int(k)}K" if k.is_integer() else f"{k:g}K"
    return f"{n:,}"


def extract_reference(text: str) -> str | None:
    """Return the longest 5-11 digit reference, ignoring spaces/hyphens."""
    if not text:
        return None
    cleaned = URL_RE.sub(" ", text).translate(MY_DIGITS)
    candidates = [re.sub(r"[ \t-]", "", x) for x in REFERENCE_RE.findall(cleaned)]
    candidates = [x for x in candidates if 5 <= len(x) <= 12]
    if not candidates:
        return None
    raw_ref = max(candidates, key=len)
    return normalize_phone_reference(raw_ref)


def reply_reference(msg: dict) -> str | None:
    """Return a reference using quote and parent message resolution.

    If a quote is a substring or partial selection of the parent message's phone number,
    the full parent reference is prioritized to prevent partial number duplicates.
    """
    reply = msg.get("reply_to_message") or {}
    parent_ref = extract_reference(reply.get("text") or reply.get("caption") or "")

    quote = msg.get("quote") if ("quote" in msg and msg.get("quote") is not None) else None
    quote_ref = extract_reference(quote.get("text") or "") if isinstance(quote, dict) else None

    if quote_ref and parent_ref:
        if is_reference_match(quote_ref, parent_ref):
            return parent_ref
        return quote_ref

    if quote_ref:
        return quote_ref
    return parent_ref
