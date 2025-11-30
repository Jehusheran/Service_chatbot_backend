# app/db.py
"""
Database initialization helpers for PostgreSQL on Railway.

Usage (async, e.g. Quart/FastAPI):
    from app.db import AsyncSessionLocal, get_session, init_db
    async with get_session() as session:
        ...
    await init_db()

Usage (sync, e.g. Flask/scripts):
    from app.db import get_sync_session, init_db_sync
    with get_sync_session() as session:
        ...
    init_db_sync()
"""

from __future__ import annotations

import os
import inspect
from typing import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from dotenv import load_dotenv

# Load .env for local dev; on Railway, env vars are injected automatically
load_dotenv()

from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# -------------------------------------------------
# Database URL (env override, Railway default)
# -------------------------------------------------
RAW_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:ngmnfPTCQoBaQZQvGsPHXPulcGNBXPUv@hopper.proxy.rlwy.net:45611/railway",
)

# For async SQLAlchemy, use postgresql+asyncpg://...
if RAW_DATABASE_URL.startswith("postgresql+asyncpg://"):
    ASYNC_DATABASE_URL = RAW_DATABASE_URL
else:
    ASYNC_DATABASE_URL = RAW_DATABASE_URL.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )

SQL_ECHO = os.getenv("SQL_ECHO", "false").lower() in ("1", "true", "yes")

# -------------------------
# Async engine & session
# -------------------------
async_engine: AsyncEngine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=SQL_ECHO,
    future=True,
)

AsyncSessionLocal = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# -------------------------
# Sync engine & session
# -------------------------
sync_engine: Engine = create_engine(
    RAW_DATABASE_URL,
    echo=SQL_ECHO,
    future=True,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False,
    class_=Session,
)

# -----------------------
# Initialization helpers
# -----------------------
async def init_db() -> None:
    """Create tables using the async engine."""
    async with async_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


def init_db_sync(drop_first: bool = False) -> None:
    """Create tables using the sync engine."""
    if drop_first:
        SQLModel.metadata.drop_all(bind=sync_engine)
    SQLModel.metadata.create_all(bind=sync_engine)

# -----------------------
# Session providers
# -----------------------
@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


@contextmanager
def get_sync_session() -> Generator[Session, None, None]:
    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()

# -----------------------
# Engine accessors
# -----------------------
def get_async_engine() -> AsyncEngine:
    return async_engine


def get_sync_engine() -> Engine:
    return sync_engine

# -----------------------
# Cleanup helpers
# -----------------------
async def dispose_engines() -> None:
    """Dispose both async and sync engines (useful on shutdown)."""
    try:
        dispose_fn = getattr(async_engine, "dispose", None)
        if dispose_fn is not None:
            if inspect.iscoroutinefunction(dispose_fn):
                await dispose_fn()  # type: ignore[arg-type]
            else:
                dispose_fn()
    except Exception:
        try:
            if hasattr(async_engine, "sync_engine"):
                async_engine.sync_engine.dispose()  # type: ignore[attr-defined]
        except Exception:
            pass

    try:
        sync_engine.dispose()
    except Exception:
        pass
