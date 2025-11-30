# app/services/__init__.py
"""
Service package re-exports.

This module tries to provide a stable import surface for service utilities used
throughout the codebase. It will attempt to import modules from the following
locations (in order):

  1. app.services.<module>   # if the module lives inside the services package
  2. app.<module>            # if the module lives at top-level app/<module>.py

If a module cannot be imported, the name is set to None so import-time errors
are avoided; real errors will surface the moment the missing object is used.
"""

from __future__ import annotations
import importlib
import logging
from typing import Optional

logger = logging.getLogger("app.services")

# List of common service modules your project references.
_CANDIDATES = [
    "llm",
    "notifications",
    "salesiq_integration",
    "google_calendar",
    "twilio_client",
    "email_service",   # if you renamed app/email.py -> app/email_service.py
        # fallback if still named email.py (but collides w/ stdlib)
    "sms",             # generic name variants
    "payments",
    "websocket_manager",
    "search",          # embedding/search helpers
]

# Populate module variables dynamically if available
_globals = globals()
for name in _CANDIDATES:
    mod = None
    # Try package-local import first: app.services.<name>
    try:
        mod = importlib.import_module(f"app.services.{name}")
        logger.debug("Imported app.services.%s", name)
    except Exception:
        # Try top-level app.<name>
        try:
            mod = importlib.import_module(f"app.{name}")
            logger.debug("Imported app.%s", name)
        except Exception:
            mod = None
            logger.debug("Service module %s not found in app.services or app.*", name)
    # Expose module or None
    _globals[name] = mod

# For backward compatibility, provide common aliases:
# e.g. llm might be at app.llm and also listed above
if "llm" not in _globals or _globals.get("llm") is None:
    try:
        import app.llm as _llm  # type: ignore
        _globals["llm"] = _llm
    except Exception:
        pass

# Build __all__ with only the names we want to export
__all__ = [n for n in _CANDIDATES if _globals.get(n) is not None]
