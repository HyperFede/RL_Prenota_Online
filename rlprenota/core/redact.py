"""Masks health/personal identifiers before anything is logged or stored as diagnostics."""
import logging
import re

_DIGIT = "[0-9LMNPQRSTUV]"  # codice fiscale "omocodia" replaces digits with these letters
PATTERNS = [
    ("CF", re.compile(rf"\b[A-Z]{{6}}{_DIGIT}{{2}}[A-Z]{_DIGIT}{{2}}[A-Z]{_DIGIT}{{3}}[A-Z]\b", re.IGNORECASE)),
    # Electronic prescription (NRE, e.g. 0300A1234567890), red prescription / IUP codes
    ("RICETTA", re.compile(r"\b\d{4}[A-Z]\d{10}\b|\b\d{15,16}\b", re.IGNORECASE)),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b")),
    ("TEL", re.compile(r"(?:(?:\+|00)39[\s.-]?)?\b(?:3\d{2}[\s.-]?\d{3}[\s.-]?\d{3,4}|0\d{1,3}[\s.-]?\d{5,8})\b")),
]


def redact(text):
    if not text:
        return text
    for label, pattern in PATTERNS:
        text = pattern.sub(f"[{label}]", text)
    return text


class RedactingFilter(logging.Filter):
    """Attach to every handler/logger: formats the record, then masks identifiers."""

    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = None
        return True
