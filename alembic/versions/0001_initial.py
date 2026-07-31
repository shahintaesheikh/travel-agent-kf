"""Initial migration — full schema from PRD Appendix C.

Revision ID: 0001_initial
Create Date: 2026-07-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extensions ──────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── users ───────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
    )

    # ── trips ───────────────────────────────────────────────────────────────
    op.create_table(
        "trips",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("destination", sa.String(255), nullable=False),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Enum("draft", "active", "awaiting_confirmation",
                                    "confirmed_taken", "archived", name="trip_status"),
                  nullable=False, server_default="draft"),
        sa.Column("budget_total_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_per_day", sa.Float, nullable=True),
        sa.Column("pct_days_with_activity", sa.Float, nullable=True),
        sa.Column("avg_meal_price_level", sa.Float, nullable=True),
        sa.Column("spend_flight", sa.Numeric(12, 2), nullable=True),
        sa.Column("spend_lodging", sa.Numeric(12, 2), nullable=True),
        sa.Column("spend_food_est", sa.Numeric(12, 2), nullable=True),
        sa.Column("trip_length", sa.Integer, nullable=True),
        sa.Column("season", sa.String(50), nullable=True),
        sa.Column("archetype", sa.Enum("city", "beach", "roadtrip", name="trip_archetype"), nullable=True),
    )

    # ── saved_items ─────────────────────────────────────────────────────────
    op.create_table(
        "saved_items",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.Enum("venue", "intent", name="saved_item_kind"), nullable=False),
        sa.Column("resolution", sa.Enum("exact_venue", "operator_unknown", "locality_only",
                                        name="saved_item_resolution"), nullable=False),
        sa.Column("geo_precision", sa.Enum("point", "locality", "region", name="geo_precision"),
                  nullable=False),
        sa.Column("google_place_id", sa.String(255), nullable=True),
        sa.Column("activity_type", sa.String(255), nullable=True),
        sa.Column("locality_id", UUID, sa.ForeignKey("saved_items.id"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("address", sa.Text, nullable=True),
        sa.Column("geom", Geography("POINT", srid=4326), nullable=False),
        sa.Column("radius_m", sa.Float, nullable=True),
        sa.Column("category", sa.String(255), nullable=True),
        sa.Column("attrs", JSONB, nullable=True),
        sa.Column("vibe_text", sa.Text, nullable=True),
        sa.Column("vibe_embedding", Vector(1536), nullable=True),
        sa.Column("transcript", sa.Text, nullable=True),
        sa.Column("ocr_text", sa.Text, nullable=True),
        sa.Column("frames_path", sa.Text, nullable=True),
        sa.Column("source", sa.String(50), nullable=True),
        sa.Column("source_video_url", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("description_model", sa.String(100), nullable=True),
        sa.Column("description_version", sa.String(50), nullable=True),
        sa.Column("trip_id", UUID, sa.ForeignKey("trips.id"), nullable=True),
        sa.Column("saved_by", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("google_place_id", name="uq_saved_items_google_place_id"),
    )
    # GIST index on geom created automatically by geoalchemy2 (spatial_index=True)

    # ── itinerary_items ─────────────────────────────────────────────────────
    op.create_table(
        "itinerary_items",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trip_id", UUID, sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("day", sa.Integer, nullable=False),
        sa.Column("slot", sa.String(20), nullable=False),  # morning | afternoon | evening
        sa.Column("tier", sa.Enum("actionable", "reference", name="itinerary_tier"), nullable=False),
        sa.Column("type", sa.Enum("flight", "lodging", "restaurant", "activity", "event",
                                  name="itinerary_type"), nullable=False),
        sa.Column("saved_item_id", UUID, sa.ForeignKey("saved_items.id"), nullable=True),
        sa.Column("raw_payload", JSONB, nullable=True),
        sa.Column("normalized", JSONB, nullable=True),
        sa.Column("selected_flights_json", JSONB, nullable=True),
        sa.Column("booking_request", JSONB, nullable=True),
        sa.Column("options_partial", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("price_amount_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("price_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_basis", sa.Enum("actual", "price_level_estimate", "uncounted",
                                         name="price_basis"),
                  nullable=False, server_default="uncounted"),
        sa.Column("handed_off_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("diverged", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("original_snapshot", JSONB, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True,
                  onupdate=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── approvals ───────────────────────────────────────────────────────────
    op.create_table(
        "approvals",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", UUID, sa.ForeignKey("itinerary_items.id"), nullable=True),
        sa.Column("trip_id", UUID, sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("scope", sa.Enum("plan", "item", name="approval_scope"), nullable=False),
        sa.Column("proposed_by", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Enum("approved", "vetoed", "expired", name="approval_outcome"),
                  nullable=True),
    )

    # ── memories ────────────────────────────────────────────────────────────
    op.create_table(
        "memories",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.Enum("trait", "intent", name="memory_kind"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", UUID, sa.ForeignKey("memories.id"), nullable=True),
    )

    # ── handoffs ────────────────────────────────────────────────────────────
    op.create_table(
        "handoffs",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", UUID, sa.ForeignKey("itinerary_items.id"), nullable=False),
        sa.Column("resolved_price_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("vendor", sa.String(255), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),  # POST or GET
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── ingest_jobs ─────────────────────────────────────────────────────────
    op.create_table(
        "ingest_jobs",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("status", sa.Enum("queued", "processing", "done", "failed",
                                    name="ingest_job_status"),
                  nullable=False, server_default="queued"),
        sa.Column("stage", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("saved_item_id", UUID, sa.ForeignKey("saved_items.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ingest_jobs")
    op.drop_table("handoffs")
    op.drop_table("memories")
    op.drop_table("approvals")
    op.drop_table("itinerary_items")
    # GIST index dropped automatically by geoalchemy2
    op.drop_table("saved_items")
    op.drop_table("trips")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS trip_archetype")
    op.execute("DROP TYPE IF EXISTS approval_outcome")
    op.execute("DROP TYPE IF EXISTS approval_scope")
    op.execute("DROP TYPE IF EXISTS ingest_job_status")
    op.execute("DROP TYPE IF EXISTS memory_kind")
    op.execute("DROP TYPE IF EXISTS price_basis")
    op.execute("DROP TYPE IF EXISTS itinerary_type")
    op.execute("DROP TYPE IF EXISTS itinerary_tier")
    op.execute("DROP TYPE IF EXISTS geo_precision")
    op.execute("DROP TYPE IF EXISTS saved_item_resolution")
    op.execute("DROP TYPE IF EXISTS saved_item_kind")
    op.execute("DROP TYPE IF EXISTS trip_status")