"""Seed script — populates the database with ~20 saved_items across two cities.

This is what removes ingestion from the critical path: downstream lanes can
test against seeded data without needing the ingestion pipeline.

Usage:
    python scripts/seed.py
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.models.db import SavedItem, User
from app.shared.db import async_session_maker

# ── Seed data ───────────────────────────────────────────────────────────────

# Two users
USERS = [
    {"id": "00000000-0000-0000-0000-000000000001", "name": "Alice"},
    {"id": "00000000-0000-0000-0000-000000000002", "name": "Bob"},
]

# ~20 saved_items across Lisbon and Tokyo
SAVED_ITEMS = [
    # ── Lisbon — exact venues ───────────────────────────────────────────────
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJ_8jX7qMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Time Out Market",
        "address": "Av. 24 de Julho 49, 1200-479 Lisboa",
        "lat": 38.7069, "lon": -9.1457,
        "radius_m": None,
        "category": "restaurant",
        "attrs": {"cuisine": "multiple", "price_level": 2},
        "vibe_text": "Huge food hall with every Portuguese specialty under one roof.",
        "confidence": 0.95,
        "source": "manual",
        "source_video_url": None,
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJ0w0x0qMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Pastéis de Belém",
        "address": "R. de Belém 84-92, 1300-085 Lisboa",
        "lat": 38.6975, "lon": -9.2040,
        "radius_m": None,
        "category": "restaurant",
        "attrs": {"cuisine": "portuguese", "price_level": 1},
        "vibe_text": "The original pastel de nata bakery since 1837. Expect a queue.",
        "confidence": 0.98,
        "source": "manual",
        "source_video_url": None,
        "saved_by": USERS[1]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJb0Zq0aMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "LX Factory",
        "address": "R. Rodrigues de Faria 103, 1300-501 Lisboa",
        "lat": 38.7035, "lon": -9.1776,
        "radius_m": None,
        "category": "attraction",
        "attrs": {"type": "market", "free": True},
        "vibe_text": "Creative hub in a former factory — shops, cafes, street art.",
        "confidence": 0.90,
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12345",
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "operator_unknown",
        "geo_precision": "point",
        "google_place_id": "ChIJX0y0zqMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Miradouro da Graça",
        "address": "Largo da Graça, 1170-165 Lisboa",
        "lat": 38.7175, "lon": -9.1280,
        "radius_m": None,
        "category": "attraction",
        "attrs": {"type": "viewpoint", "free": True},
        "vibe_text": "Sunset viewpoint with a terrace cafe. Best view of the castle.",
        "confidence": 0.70,  # operator_unknown → lower confidence
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12346",
        "saved_by": USERS[1]["id"],
    },
    # ── Lisbon — intents / locality_only ────────────────────────────────────
    {
        "id": uuid4(),
        "kind": "intent",
        "resolution": "locality_only",
        "geo_precision": "locality",
        "google_place_id": None,
        "activity_type": "parasailing",
        "locality_id": None,
        "name": "Parasailing in Lisbon",
        "address": "Lisbon coast",
        "lat": 38.7223, "lon": -9.1393,
        "radius_m": 5000.0,
        "category": "activity",
        "attrs": {"activity": "parasailing", "season": "summer"},
        "vibe_text": "Parasailing along the Lisbon coastline — looks thrilling.",
        "confidence": 0.55,
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12347",
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "intent",
        "resolution": "locality_only",
        "geo_precision": "locality",
        "google_place_id": None,
        "activity_type": "fado",
        "locality_id": None,
        "name": "Fado night in Alfama",
        "address": "Alfama, Lisbon",
        "lat": 38.7115, "lon": -9.1288,
        "radius_m": 1000.0,
        "category": "activity",
        "attrs": {"activity": "fado", "type": "cultural"},
        "vibe_text": "Traditional Fado music in Alfama's narrow streets.",
        "confidence": 0.60,
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12348",
        "saved_by": USERS[1]["id"],
    },
    # ── Tokyo — exact venues ────────────────────────────────────────────────
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJL4t9m0GOGNUR8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Tsukiji Outer Market",
        "address": "4-16-2 Tsukiji, Chuo-ku, Tokyo",
        "lat": 35.6654, "lon": 139.7707,
        "radius_m": None,
        "category": "restaurant",
        "attrs": {"cuisine": "seafood", "price_level": 2},
        "vibe_text": "Busy market street with fresh sushi, street food, and kitchen knives.",
        "confidence": 0.97,
        "source": "manual",
        "source_video_url": None,
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJX0y0zqMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Senso-ji Temple",
        "address": "2-3-1 Asakusa, Taito-ku, Tokyo",
        "lat": 35.7148, "lon": 139.7967,
        "radius_m": None,
        "category": "attraction",
        "attrs": {"type": "temple", "free": True},
        "vibe_text": "Tokyo's oldest temple. Nakamise-dori leads to the main hall.",
        "confidence": 0.95,
        "source": "manual",
        "source_video_url": None,
        "saved_by": USERS[1]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJb0Zq0aMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Shibuya Sky",
        "address": "Shibuya Scramble Square, 2-24-12 Shibuya, Tokyo",
        "lat": 35.6595, "lon": 139.7010,
        "radius_m": None,
        "category": "attraction",
        "attrs": {"type": "observation_deck", "price": 2000},
        "vibe_text": "Open-air observation deck with 360° views of Tokyo.",
        "confidence": 0.92,
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12349",
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJ0w0x0qMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Ichiran Ramen (Shinjuku)",
        "address": "1-22-7 Jinnan, Shibuya-ku, Tokyo",
        "lat": 35.6916, "lon": 139.7036,
        "radius_m": None,
        "category": "restaurant",
        "attrs": {"cuisine": "ramen", "price_level": 1},
        "vibe_text": "Famous solo-booth tonkotsu ramen. Order via vending machine.",
        "confidence": 0.99,
        "source": "manual",
        "source_video_url": None,
        "saved_by": USERS[1]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "operator_unknown",
        "geo_precision": "point",
        "google_place_id": "ChIJX0y0zqMhJQ0R8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "Golden Gai Bar Hopping",
        "address": "Shinjuku, Tokyo",
        "lat": 35.6903, "lon": 139.7042,
        "radius_m": None,
        "category": "attraction",
        "attrs": {"type": "nightlife", "free": False},
        "vibe_text": "Tiny bars in narrow alleys — each seats 3-5 people.",
        "confidence": 0.65,  # operator_unknown
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12350",
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "exact_venue",
        "geo_precision": "point",
        "google_place_id": "ChIJL4t9m0GOGNUR8Q6q0QAAAAA",
        "activity_type": None,
        "locality_id": None,
        "name": "teamLab Borderless",
        "address": "1-3-1 Aomi, Koto-ku, Tokyo",
        "lat": 35.6264, "lon": 139.7839,
        "radius_m": None,
        "category": "attraction",
        "attrs": {"type": "museum", "price": 3800, "booking_required": True},
        "vibe_text": "Immersive digital art museum. Book tickets weeks ahead.",
        "confidence": 0.93,
        "source": "manual",
        "source_video_url": None,
        "saved_by": USERS[1]["id"],
    },
    # ── Tokyo — intents / locality_only ─────────────────────────────────────
    {
        "id": uuid4(),
        "kind": "intent",
        "resolution": "locality_only",
        "geo_precision": "locality",
        "google_place_id": None,
        "activity_type": "onsen",
        "locality_id": None,
        "name": "Onsen experience in Hakone",
        "address": "Hakone, Japan",
        "lat": 35.2325, "lon": 139.1067,
        "radius_m": 10000.0,
        "category": "activity",
        "attrs": {"activity": "onsen", "day_trip_from": "Tokyo"},
        "vibe_text": "Hot spring bath with Mt. Fuji view. Day trip from Tokyo.",
        "confidence": 0.58,
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12351",
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "intent",
        "resolution": "locality_only",
        "geo_precision": "locality",
        "google_place_id": None,
        "activity_type": "karaoke",
        "locality_id": None,
        "name": "Karaoke in Shibuya",
        "address": "Shibuya, Tokyo",
        "lat": 35.6595, "lon": 139.7010,
        "radius_m": 2000.0,
        "category": "activity",
        "attrs": {"activity": "karaoke", "type": "nightlife"},
        "vibe_text": "Private karaoke room chains all over Shibuya.",
        "confidence": 0.62,
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12352",
        "saved_by": USERS[1]["id"],
    },
    # ── Low-confidence items ────────────────────────────────────────────────
    {
        "id": uuid4(),
        "kind": "venue",
        "resolution": "operator_unknown",
        "geo_precision": "point",
        "google_place_id": None,
        "activity_type": None,
        "locality_id": None,
        "name": "Unknown Ramen Shop (Shinjuku)",
        "address": "Shinjuku, Tokyo",
        "lat": 35.6900, "lon": 139.7020,
        "radius_m": None,
        "category": "restaurant",
        "attrs": {"cuisine": "ramen"},
        "vibe_text": "Small ramen shop shown in a TikTok — name not visible.",
        "confidence": 0.30,  # low confidence — needs user resolution
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12353",
        "saved_by": USERS[0]["id"],
    },
    {
        "id": uuid4(),
        "kind": "intent",
        "resolution": "locality_only",
        "geo_precision": "locality",
        "google_place_id": None,
        "activity_type": "cooking_class",
        "locality_id": None,
        "name": "Portuguese cooking class",
        "address": "Lisbon, Portugal",
        "lat": 38.7223, "lon": -9.1393,
        "radius_m": 3000.0,
        "category": "activity",
        "attrs": {"activity": "cooking_class", "cuisine": "portuguese"},
        "vibe_text": "Learn to make pastel de nata and bacalhau.",
        "confidence": 0.45,
        "source": "tiktok",
        "source_video_url": "https://www.tiktok.com/@example/video/12354",
        "saved_by": USERS[1]["id"],
    },
]


async def seed() -> None:
    """Populate the database with seed data."""
    async with async_session_maker() as session:
        # Check if data already exists
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none():
            print("Database already seeded. Skipping.")
            return

        # Insert users
        for user_data in USERS:
            session.add(User(id=user_data["id"], name=user_data["name"]))
        await session.flush()
        print(f"Created {len(USERS)} users.")

        # Insert saved items
        for item_data in SAVED_ITEMS:
            lat = item_data.pop("lat")
            lon = item_data.pop("lon")
            session.add(
                SavedItem(
                    **item_data,
                    geom=f"SRID=4326;POINT({lon} {lat})",
                )
            )
        await session.flush()
        print(f"Created {len(SAVED_ITEMS)} saved items.")

        await session.commit()
        print("Seed complete.")


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
