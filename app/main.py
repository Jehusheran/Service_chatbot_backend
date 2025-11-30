# app/main.py
"""
Flask entrypoint for Service Chatbot backend.

Usage (dev):
  # from project root
  export FLASK_APP=app.main
  export FLASK_ENV=development
  flask run --host=127.0.0.1 --port=8000

Or run directly (dev):
  python -m app.main

Production:
  gunicorn --bind 0.0.0.0:8000 "app.main:create_app()"
"""
from __future__ import annotations

import os
import sys
import threading
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

# Make sure project root is on sys.path when running as script/module.
if __name__ == "__main__" and __package__ is None:
    project_root = Path(__file__).resolve().parent
    if str(project_root.parent) not in sys.path:
        sys.path.insert(0, str(project_root.parent))

load_dotenv()

from flask import Flask, jsonify
from flask_cors import CORS

logger = logging.getLogger("app.main")

# Import your modules using absolute imports
# init_db may be async or sync depending on your db layer
try:
    from app.db import init_db  # type: ignore
except Exception:
    def init_db():
        return None  # fallback no-op

# Routers (these modules should export a Flask Blueprint as `bp` or `router`)
try:
    from app.routers import auth_routes, chat_routes, schedule_routes, summary_routes  # type: ignore
except Exception as e:
    # We'll log below when attempting to register
    auth_routes = chat_routes = schedule_routes = summary_routes = None
    logger.debug("Could not import one or more router modules at import-time: %s", e)

# SalesIQ integration blueprint (may live under app.services or app.services.salesiq_integration)
try:
    from app.services import salesiq_integration  # type: ignore
except Exception:
    salesiq_integration = None


def register_blueprint_if_possible(app: Flask, mod, url_prefix: str = ""):
    """
    Register a Flask Blueprint if the module exports `bp`, `router`, or `blueprint`.
    Raise descriptive error if not found.
    """
    if mod is None:
        raise RuntimeError("Module is None; cannot register blueprint.")
    bp = getattr(mod, "bp", None) or getattr(mod, "router", None) or getattr(mod, "blueprint", None)
    if bp is None:
        raise RuntimeError(f"No blueprint found in module {mod.__name__}. Export a Flask Blueprint as 'bp' or 'router'.")
    app.register_blueprint(bp, url_prefix=url_prefix)


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)

    # Basic config
    app.config["ENV"] = os.getenv("FLASK_ENV", "production")
    app.config["DEBUG"] = os.getenv("FLASK_ENV", "development") == "development"
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "devsecret")

    # Configure logging to show helpful messages during init
    if app.config["DEBUG"]:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # CORS
    origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://service-chatbot-frontend-coral.vercel.app",
        os.getenv("FRONTEND_URL", "")
    ]
    CORS(app, origins=[o for o in origins if o], supports_credentials=True)

    # Register routers (expecting Flask Blueprints exported as `bp`/`router`)
    try:
        register_blueprint_if_possible(app, auth_routes, url_prefix="/v1/auth")
    except Exception as e:
        app.logger.warning("Auth routes not registered: %s", e)

    try:
        register_blueprint_if_possible(app, chat_routes, url_prefix="/v1/chat")
    except Exception as e:
        app.logger.warning("Chat routes not registered: %s", e)

    try:
        register_blueprint_if_possible(app, schedule_routes, url_prefix="/v1/schedule")
    except Exception as e:
        app.logger.warning("Schedule routes not registered: %s", e)

    try:
        register_blueprint_if_possible(app, summary_routes, url_prefix="/v1/summary")
    except Exception as e:
        app.logger.warning("Summary routes not registered: %s", e)

    # SalesIQ integration blueprint (webhook)
    try:
        register_blueprint_if_possible(app, salesiq_integration, url_prefix="/v1/salesiq")
    except Exception as e:
        app.logger.warning("SalesIQ integration blueprint not registered: %s", e)

    # -------------------------
    # DB init on first request (robust and safe)
    # -------------------------
    init_lock = threading.Lock()

    def _maybe_init_db_sync():
        """
        Prefer a synchronous DB init if available (init_db_sync on app.db).
        Fallback to calling init_db() (async) inside a fresh event loop.
        This avoids using asyncio.get_event_loop() inside random worker threads.
        Also detects the common 'greenlet' missing error and logs actionable advice.
        """
        from importlib import import_module

        # Try to prefer a sync initializer if available
        try:
            db_mod = import_module("app.db")
        except Exception:
            db_mod = None

        if db_mod and hasattr(db_mod, "init_db_sync"):
            try:
                app.logger.info("Attempting synchronous DB init via app.db.init_db_sync()")
                db_mod.init_db_sync()
                app.logger.info("Database initialized via init_db_sync().")
                return
            except Exception as e:
                app.logger.exception("init_db_sync() failed, will try async init_db(): %s", e)

        # Fallback: attempt async init_db() if provided
        try:
            res = init_db()
            # If init_db returned an awaitable / coroutine, run it in a fresh loop inside this thread
            if hasattr(res, "__await__"):
                app.logger.info("Running async init_db() in a fresh event loop (fallback path).")
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(res)
                finally:
                    # Clean up: unset and close loop
                    try:
                        asyncio.set_event_loop(None)
                    except Exception:
                        pass
                    try:
                        loop.close()
                    except Exception:
                        pass
                app.logger.info("Database initialized (async fallback).")
            else:
                app.logger.info("init_db() executed synchronously (no awaitable returned).")
            return
        except ValueError as ve:
            # Known case: missing greenlet triggers ValueError mentioning greenlet
            msg = str(ve).lower()
            if "greenlet" in msg or "greenlet" in getattr(ve, "args", ("",))[0].lower():
                app.logger.exception("DB init failed: greenlet is required by SQLAlchemy async helpers but is not installed.")
                app.logger.error("Install it in your virtualenv and retry: pip install greenlet")
            else:
                app.logger.exception("DB init failed with ValueError: %s", ve)
            return
        except RuntimeError as re:
            # Event loop related errors; log and provide hint
            app.logger.exception("DB init runtime error (event loop problem): %s", re)
            app.logger.error("If this occurs in a worker thread, prefer init_db_sync() or install greenlet: pip install greenlet")
            return
        except Exception as e:
            app.logger.exception("DB initialization (fallback) failed: %s", e)
            return

    def _maybe_init_db_once():
        if app.config.get("_db_initialized", False):
            return
        with init_lock:
            if app.config.get("_db_initialized", False):
                return
            _maybe_init_db_sync()
            app.config["_db_initialized"] = True

    @app.before_request
    def _ensure_db_initialized():
        # Blocks the first incoming request until DB init completes (safer for demos).
        # For high-performance production, initialize DB at process start instead.
        _maybe_init_db_once()

    # Health route
    @app.route("/healthz", methods=["GET"])
    def _health():
        return jsonify({"status": "ok", "service": "service-chatbot-backend"})

    # Root
    @app.route("/", methods=["GET"])
    def root():
        return jsonify({
            "status": "ok",
            "service": "Service Chatbot Backend",
            "version": "1.0.0",
            "docs": "/apidocs"  # change to /docs if you add docs UI
        })

    return app


# For flask CLI and WSGI servers
app = create_app()

if __name__ == "__main__":
    # Quick dev runner
    host = os.getenv("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_RUN_PORT", "8000"))
    debug = app.config.get("DEBUG", False)
    app.run(host=host, port=port, debug=debug)
