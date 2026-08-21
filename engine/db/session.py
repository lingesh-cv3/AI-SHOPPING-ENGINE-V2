"""Database connection and session handling.

One module owning how a session is created and torn down, so no route ever has to
think about it.

The connection string is the only Postgres-versus-SQLite difference in the whole
codebase:

    sqlite+aiosqlite:///./cv3.db                     (default, no install)
    postgresql+asyncpg://user:pass@host/cv3_engine   (production)

Everything above this module is unaware of which is in use. That was the point of
keeping the schema free of SQLite-only features.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

logger = logging.getLogger(__name__)

#: A file next to the project rather than in memory, so a restart does not wipe
#: the approval queue. An in-memory database would make demos frustrating in
#: exactly the way persistence was meant to fix.
DEFAULT_URL = "sqlite+aiosqlite:///./cv3.db"


def database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_URL).strip() or DEFAULT_URL


_engine = create_async_engine(
    database_url(),
    # Verbose SQL on demand, off by default. Useful when a query returns something
    # surprising and you want to see what actually ran.
    echo=os.getenv("SQL_ECHO", "").lower() in {"1", "true", "yes"},
    # SQLite refuses cross-thread use by default and the async driver needs this.
    # Harmless on Postgres, which ignores it.
    connect_args={"check_same_thread": False} if "sqlite" in database_url() else {},
)

#: expire_on_commit=False so objects stay usable after the session closes. Without
#: it, reading an attribute after commit triggers a lazy refresh against a closed
#: session, which fails in async code in a confusing way.
SessionFactory = async_sessionmaker(_engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on failure.

    Every write goes through this. A half-written case is worse than no case: an
    approval queue entry pointing at a case that does not exist would be a
    genuinely confusing thing for an operator to find.
    """
    session = SessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def create_schema() -> None:
    """Create tables if they do not exist.

    Fine for development and for a single-instance demo. A real deployment wants
    Alembic migrations instead, because this cannot alter an existing table - it
    only creates missing ones, and a column added later would be silently absent.
    """
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database ready at %s", database_url().split("://")[0])


async def dispose() -> None:
    """Close the pool. Called on shutdown."""
    await _engine.dispose()