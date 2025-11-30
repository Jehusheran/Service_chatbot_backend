# app/services/__init__.py
# Package initializer for app.services
# Keep lightweight — import submodules explicitly elsewhere when needed.
__all__ = ["notifications", "salesiq_integration"]
# app/__init__.py
"""
Package initializer for app. Re-export a few top-level helper modules so older imports
like `from app import email` continue to work after renames.
"""

# try to export renamed email module under the legacy name `email`
try:
    from . import email_service as email  # prefer new module name
except Exception:
    # fallback: try to import original if still present
    try:
        from . import email as email  # type: ignore
    except Exception:
        email = None  # will raise when actually used
