"""SQLAlchemy ORM models — full DDL from PRD Appendix C.

Every table, column, and constraint is defined here. Alembic autogenerate reads
this file to produce migrations. Downstream lanes import these models, not the
reverse — nothing in `app/models/` imports from `app/travel/` or `app/agent/`.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Enums ───────────────────────────────────────────────────────────────────


class TripStatus(enum.StrEnum):
    draft = "draft"
    active = "active"
    awaiting_confirmation = "awaiting_confirmation"
    confirmed_taken = "confirmed_taken"
    archived = "archived"


class SavedItemKind(enum.StrEnum):
    venue = "venue"
    intent = "intent"


class SavedItemResolution(enum.StrEnum):
    exact_venue = "exact_venue"
    operator_unknown = "operator_unknown"
    locality_only = "locality_only"


class GeoPrecision(enum.StrEnum):
    point = "point"
    locality = "locality"
    region = "region"


class ItineraryTier(enum.StrEnum):
    actionable = "actionable"
    reference = "reference"


class ItineraryType(enum.StrEnum):
    flight = "flight"
    lodging = "lodging"
    restaurant = "restaurant"
    activity = "activity"
    event = "event"


class PriceBasis(enum.StrEnum):
    actual = "actual"
    price_level_estimate = "price_level_estimate"
    uncounted = "uncounted"


class MemoryKind(enum.StrEnum):
    trait = "trait"
    intent = "intent"


class IngestJobStatus(enum.StrEnum):
    queued = "queued"
    processing = "processing"
    done = "done"
    failed = "failed"


class ApprovalScope(enum.StrEnum):
    plan = "plan"
    item = "item"


class ApprovalOutcome(enum.StrEnum):
    approved = "approved"
    vetoed = "vetoed"
    expired = "expired"


class TripArchetype(enum.StrEnum):
    city = "city"
    beach = "beach"
    roadtrip = "roadtrip"


# ── Tables ──────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    destination: Mapped[str] = mapped_column(String(255), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[TripStatus] = mapped_column(
        Enum(TripStatus, name="trip_status", create_constraint=True),
        nullable=False,
        default=TripStatus.draft,
    )
    budget_total_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    created_by: Mapped[str] = mapped_column(
        UUID, ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Shape metrics (populated on confirmation)
    items_per_day: Mapped[float | None] = mapped_column(nullable=True)
    pct_days_with_activity: Mapped[float | None] = mapped_column(nullable=True)
    avg_meal_price_level: Mapped[float | None] = mapped_column(nullable=True)
    spend_flight: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    spend_lodging: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    spend_food_est: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    trip_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[str | None] = mapped_column(String(50), nullable=True)
    archetype: Mapped[TripArchetype | None] = mapped_column(
        Enum(TripArchetype, name="trip_archetype", create_constraint=True),
        nullable=True,
    )

    creator: Mapped[User] = relationship("User")


class SavedItem(Base):
    __tablename__ = "saved_items"
    __table_args__ = (
        UniqueConstraint("google_place_id", name="uq_saved_items_google_place_id"),
    )

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    kind: Mapped[SavedItemKind] = mapped_column(
        Enum(SavedItemKind, name="saved_item_kind", create_constraint=True),
        nullable=False,
    )
    resolution: Mapped[SavedItemResolution] = mapped_column(
        Enum(
            SavedItemResolution,
            name="saved_item_resolution",
            create_constraint=True,
        ),
        nullable=False,
    )
    geo_precision: Mapped[GeoPrecision] = mapped_column(
        Enum(GeoPrecision, name="geo_precision", create_constraint=True),
        nullable=False,
    )
    google_place_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    activity_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locality_id: Mapped[str | None] = mapped_column(
        UUID, ForeignKey("saved_items.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    geom: Mapped[object] = mapped_column(
        Geography("POINT", srid=4326), nullable=False
    )
    radius_m: Mapped[float | None] = mapped_column(nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attrs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    vibe_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    vibe_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1536), nullable=True
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    frames_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    description_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    trip_id: Mapped[str | None] = mapped_column(
        UUID, ForeignKey("trips.id"), nullable=True, default=None
    )
    saved_by: Mapped[str] = mapped_column(
        UUID, ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # GIST index on geom is created in the migration


class ItineraryItem(Base):
    __tablename__ = "itinerary_items"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    trip_id: Mapped[str] = mapped_column(
        UUID, ForeignKey("trips.id"), nullable=False
    )
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    slot: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # morning | afternoon | evening
    tier: Mapped[ItineraryTier] = mapped_column(
        Enum(ItineraryTier, name="itinerary_tier", create_constraint=True),
        nullable=False,
    )
    type: Mapped[ItineraryType] = mapped_column(
        Enum(ItineraryType, name="itinerary_type", create_constraint=True),
        nullable=False,
    )
    saved_item_id: Mapped[str | None] = mapped_column(
        UUID, ForeignKey("saved_items.id"), nullable=True
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    normalized: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    selected_flights_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    booking_request: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    options_partial: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    price_amount_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    price_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    price_basis: Mapped[PriceBasis] = mapped_column(
        Enum(PriceBasis, name="price_basis", create_constraint=True),
        nullable=False,
        default=PriceBasis.uncounted,
    )
    handed_off_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    diverged: Mapped[bool] = mapped_column(nullable=False, default=False)
    original_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    item_id: Mapped[str | None] = mapped_column(
        UUID, ForeignKey("itinerary_items.id"), nullable=True
    )
    trip_id: Mapped[str] = mapped_column(
        UUID, ForeignKey("trips.id"), nullable=False
    )
    scope: Mapped[ApprovalScope] = mapped_column(
        Enum(ApprovalScope, name="approval_scope", create_constraint=True),
        nullable=False,
    )
    proposed_by: Mapped[str] = mapped_column(
        UUID, ForeignKey("users.id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome: Mapped[ApprovalOutcome | None] = mapped_column(
        Enum(ApprovalOutcome, name="approval_outcome", create_constraint=True),
        nullable=True,
    )


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(
        UUID, ForeignKey("users.id"), nullable=False
    )
    kind: Mapped[MemoryKind] = mapped_column(
        Enum(MemoryKind, name="memory_kind", create_constraint=True),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by: Mapped[str | None] = mapped_column(
        UUID, ForeignKey("memories.id"), nullable=True
    )


class Handoff(Base):
    __tablename__ = "handoffs"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    item_id: Mapped[str] = mapped_column(
        UUID, ForeignKey("itinerary_items.id"), nullable=False
    )
    resolved_price_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    vendor: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # "POST" or "GET"
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestJob(Base):
    __tablename__ = "ingest_jobs"

    id: Mapped[str] = mapped_column(UUID, primary_key=True, default=uuid4)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[IngestJobStatus] = mapped_column(
        Enum(IngestJobStatus, name="ingest_job_status", create_constraint=True),
        nullable=False,
        default=IngestJobStatus.queued,
    )
    stage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    saved_item_id: Mapped[str | None] = mapped_column(
        UUID, ForeignKey("saved_items.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
