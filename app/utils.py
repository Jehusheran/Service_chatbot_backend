# app/utils.py
"""
General-purpose utilities used across the project:
 - ID / token generation
 - Time formatting (ISO8601 UTC)
 - Validation helpers (email, phone)
 - Hashing for cache keys
 - JSON-safe operations
 - Conversation export for LLM summaries
 - Logging utilities
 - Async sleep wrappers (for retry patterns)

This file has ZERO external dependencies (safe on Python 3.11–3.14).
"""
from __future__ import annotations

import re
import os
import json
import time
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# -------------------------------------------------------------
# TIME HELPERS
# -------------------------------------------------------------
def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """Convert datetime to RFC3339/ISO8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_iso(s: str) -> datetime:
    """Parse ISO8601 string to datetime."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return utcnow()


# -------------------------------------------------------------
# RANDOM + SECURE STRING HELPERS
# -------------------------------------------------------------
def generate_id(prefix: str = "id") -> str:
    """Generate a short unique ID like 'id_32fa9ab1e'."""
    return f"{prefix}_{secrets.token_hex(5)}"


def generate_uuid() -> str:
    """Full-length random hex UUID (not RFC4122, but enough for IDs)."""
    return secrets.token_hex(16)


def generate_otp(n: int = 6) -> str:
    """Generate numeric OTP."""
    alphabet = "0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def secure_random_string(n: int = 16) -> str:
    """Random alphanumeric string."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


# -------------------------------------------------------------
# HASHING + CACHE KEYS
# -------------------------------------------------------------
def sha256(text: str) -> str:
    """Return SHA256 hex digest."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key_from_messages(messages: List[Dict[str, Any]]) -> str:
    """
    Generate a stable hash for caching LLM summaries.
    """
    serialized = json.dumps(messages, default=str, sort_keys=True)
    return sha256(serialized)


# -------------------------------------------------------------
# VALIDATION HELPERS
# -------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")


def is_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s or ""))


def is_phone(s: str) -> bool:
    return bool(PHONE_RE.match(s or ""))


# -------------------------------------------------------------
# JSON HELPERS
# -------------------------------------------------------------
def safe_json(obj: Any, indent: Optional[int] = None) -> str:
    """Convert to JSON, ignoring invalid objects."""
    try:
        return json.dumps(obj, default=str, indent=indent)
    except Exception:
        return "{}"


def from_json(s: str, default: Any = None) -> Any:
    """Safe JSON loader."""
    try:
        return json.loads(s)
    except Exception:
        return default


# -------------------------------------------------------------
# CONVERSATION EXPORT (Used for LLM Summaries)
# -------------------------------------------------------------
def export_messages_to_text(messages: List[Dict[str, Any]]) -> str:
    """
    Convert message list → plain text, ready for prompt context.

    Each line:
       [timestamp] sender: message
    """
    lines = []
    for m in messages:
        ts = m.get("created_at")
        sender = m.get("sender", "unknown")
        msg = m.get("message", "")

        if isinstance(ts, datetime):
            ts = ts.isoformat()
        elif not ts:
            ts = utcnow().isoformat()

        lines.append(f"[{ts}] {sender}: {msg}")

    return "\n".join(lines)


# -------------------------------------------------------------
# LOGGING HELPERS
# -------------------------------------------------------------
def log_info(msg: str):
    print(f"[INFO {utcnow().isoformat()}] {msg}")


def log_error(msg: str):
    print(f"[ERROR {utcnow().isoformat()}] {msg}")


def log_debug(msg: str):
    if os.getenv("DEBUG", "0") == "1":
        print(f"[DEBUG {utcnow().isoformat()}] {msg}")


# -------------------------------------------------------------
# ASYNC UTILITY HELPERS
# -------------------------------------------------------------
async def async_sleep(seconds: float):
    """
    Async wrapper around sleep (useful for retry timing).
    """
    import asyncio
    await asyncio.sleep(seconds)


async def retry_async(func, attempts: int = 3, delay: float = 1.0):
    """
    Retry an async function N times.
    Example:
        result = await retry_async(lambda: call_api(), attempts=3)
    """
    for i in range(attempts):
        try:
            return await func()
        except Exception:
            if i == attempts - 1:
                raise
            await async_sleep(delay)
