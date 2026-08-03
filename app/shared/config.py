"""Application configuration via pydantic-settings.

One `DATABASE_URL` in; sync + async driver URLs out.
`postgres://` → `postgresql://` normalization.
"""

from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # `extra="ignore"`, not the pydantic-settings default of "forbid". A real
    # deployment's environment carries variables this app doesn't read, and
    # forbidding them raised at *import* time — which took down every module
    # importing `settings`, including the test suite. Worse, the resulting
    # ValidationError prints the offending value, so a stray provider key
    # ended up in tracebacks and logs.
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

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
    def _database_url_bare(self) -> str:
        """`postgresql://...` with any driver qualifier stripped."""
        url = self.database_url
        for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql+psycopg2://"):
            if url.startswith(prefix):
                return url.replace(prefix, "postgresql://", 1)
        return url

    @property
    def database_url_sync(self) -> str:
        """Sync driver URL for Alembic and the LangGraph Postgres checkpointer.

        The driver must be named explicitly. A bare `postgresql://` makes
        SQLAlchemy reach for psycopg2, which this project does not install —
        `pyproject.toml` pins `psycopg[binary]` (psycopg3) for exactly these
        two consumers.
        """
        return self._database_url_bare.replace("postgresql://", "postgresql+psycopg://", 1)

    @property
    def database_url_async(self) -> str:
        """Async driver URL (asyncpg) for the app."""
        return self._database_url_bare.replace("postgresql://", "postgresql+asyncpg://", 1)

    # ── Redis ───────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Provider mode ───────────────────────────────────────────────────────
    provider_mode: str = "replay"  # live | record | replay

    # ── Provider credentials ────────────────────────────────────────────────
    # SecretStr so a key never renders in a repr, traceback or log line. Read
    # with `.get_secret_value()` at the call site, never earlier.
    # Optional: replay mode needs no credentials, and that is the default.
    serpapi_api_key: SecretStr | None = None  # SerpApi: `api_key` query param
    omkar_api_key: SecretStr | None = None  # omkar: `API-Key` header (exact casing)
    google_places_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None  # embeddings only
    anthropic_api_key: SecretStr | None = None

    # ── Auth ────────────────────────────────────────────────────────────────
    session_secret: str = "dev-secret-change-in-production"
    ingest_bearer_token: str = "dev-ingest-token"

    # ── Quota ───────────────────────────────────────────────────────────────
    quota_calls_per_turn: int = 6
    quota_calls_per_trip_hour: int = 40


settings = Settings()
