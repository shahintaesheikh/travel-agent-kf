"""Application configuration via pydantic-settings.

One `DATABASE_URL` in; sync + async driver URLs out.
`postgres://` → `postgresql://` normalization.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = "postgresql://travel:travel@localhost:5432/travel"

    @field_validator("database_url")
    @classmethod
    def _normalize_postgres_scheme(cls, v: str) -> str:
        """Normalize `postgres://` to `postgresql://` for SQLAlchemy compatibility."""
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql://", 1)
        return v

    @property
    def database_url_sync(self) -> str:
        """Sync driver URL (psycopg) for Alembic and LangGraph checkpointer."""
        return self.database_url.replace(
            "postgresql+asyncpg://", "postgresql://", 1
        ) if "asyncpg" in self.database_url else self.database_url

    @property
    def database_url_async(self) -> str:
        """Async driver URL (asyncpg) for the app."""
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        return self.database_url

    # ── Redis ───────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Provider mode ───────────────────────────────────────────────────────
    provider_mode: str = "replay"  # live | record | replay

    # ── Auth ────────────────────────────────────────────────────────────────
    session_secret: str = "dev-secret-change-in-production"
    ingest_bearer_token: str = "dev-ingest-token"

    # ── Quota ───────────────────────────────────────────────────────────────
    quota_calls_per_turn: int = 6
    quota_calls_per_trip_hour: int = 40


settings = Settings()
