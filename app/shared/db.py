"""Database engine and session factory.

Provides async `AsyncEngine` + `async_session_maker` for the app, and
sync `engine` for Alembic migrations.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.shared.config import settings

async_engine = create_async_engine(settings.database_url_async, echo=False)
async_session_maker = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)

# Sync engine for Alembic and LangGraph Postgres checkpointer
sync_engine = create_engine(settings.database_url_sync, echo=False)
