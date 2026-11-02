"""Redaction: a handoff bundle leaves this machine, so secrets must not ride along."""

from __future__ import annotations

import json
import re

from .model import Thread

PATTERNS = [
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("password", re.compile(r"(?i)\b(password|passwd|pwd|token|secret)\s*[=:]\s*[\"']?([^\s\"'&]{6,})")),
    ("private-key", re.compile(r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?(?:-----END \1-----|\Z)", re.DOTALL)),
]


def redact_text(text: str) -> tuple:
    if not text:
        return text, []
    hits = []
    out = text
    for name, pattern in PATTERNS:

        def replace(match, name=name):
            hits.append(name)
            if name == "password":
                return f"{match.group(1)}=[REDACTED]"
            return f"[REDACTED:{name}]"

        out = pattern.sub(replace, out)
    return out, hits


def redact_thread(thread: Thread) -> dict:
    """Mask secrets in place; returns a count per pattern."""
    counts = {}
    credential_keys = {"password", "passwd", "pwd", "token", "secret", "apikey", "accesstoken", "refreshtoken", "authorization", "clientsecret"}

    def clean(value, credential=False):
        if isinstance(value, list):
            return [clean(item, credential) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                probe = str(key).lower().replace("_", "").replace("-", "")
                result[clean(key)] = clean(item, credential or probe in credential_keys)
            return result
        if credential and value is not None and value != "":
            counts["password"] = counts.get("password", 0) + 1
            return "[REDACTED]"
        if isinstance(value, str):
            cleaned, hits = redact_text(value)
            for name in hits:
                counts[name] = counts.get(name, 0) + 1
            # Tool arguments/results may contain a second JSON layer. Preserve
            # their formatting unless decoding exposes something to mask.
            if cleaned.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(cleaned)
                except (ValueError, RecursionError):
                    pass
                else:
                    masked = clean(decoded)
                    if masked != decoded:
                        cleaned = json.dumps(masked, ensure_ascii=False)
            return cleaned
        return value

    for event in thread.events:
        for field in ("text", "name", "args", "result", "call_id", "ref"):
            setattr(event, field, clean(getattr(event, field)))
        event.raw = clean(event.raw)
    return counts
