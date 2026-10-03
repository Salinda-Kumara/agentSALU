"""Secret detection and redaction for logs, memory and UI output."""

from __future__ import annotations

import os
import re

REDACTED = "[REDACTED]"

# Environment variable names whose *values* must never appear in output.
_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE")

# (pattern, replacement). Replacements keep non-secret context (e.g. "password=").
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), REDACTED),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), REDACTED),                    # Anthropic keys / OAuth tokens
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), REDACTED),                      # generic sk- keys
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),                  # GitHub tokens
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), REDACTED),                   # GitLab
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),               # Slack
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),                          # AWS access key id
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), REDACTED),                      # Google API key
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), REDACTED),               # Telegram bot token
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), REDACTED),  # JWT
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1" + REDACTED),
    (re.compile(r"(?i)(\b(?:password|passwd|pwd|secret|api[_-]?key|token|access[_-]?key)\b\s*[:=]\s*)"
                r"(\"[^\"]*\"|'[^']*'|\S+)"), r"\1" + REDACTED),
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)"), r"\1" + REDACTED + r"\3"),  # user:pass@host
]


def _env_secret_values() -> list[str]:
    values = [v for k, v in os.environ.items()
              if v and len(v) >= 8 and any(h in k.upper() for h in _SECRET_ENV_HINTS)]
    return sorted(values, key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    for value in _env_secret_values():
        text = text.replace(value, REDACTED)
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def contains_secret(text: str) -> bool:
    return bool(text) and redact(text) != text


def _is_secret_key(key) -> bool:
    k = str(key).lower()
    return any(h in k for h in ("password", "passwd", "secret", "api_key", "apikey", "token", "private_key"))


def redact_obj(obj):
    """Recursively redact strings inside dicts/lists (and values under secret-looking keys)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: (REDACTED if _is_secret_key(k) and v else redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
