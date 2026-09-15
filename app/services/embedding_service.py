"""
Embedding Service
-----------------
Converts the Google Sheets cache (services, pricing, villas) into text
chunks and upserts them into the Pinecone index 'easybali-services'.

The index is auto-created by get_index() if it doesn't exist — no manual
Pinecone dashboard setup required.

Triggered at:
  - App startup  (main.py → on_startup)
  - POST /menu/refresh  (main_menu_routes.py)
  - POST /admin/rebuild-embeddings  (admin_routes.py)

All failures are caught and logged — this never blocks the main app.
"""

import logging
import re
from typing import Any, Dict, List

from app.services.openai_client import client
from app.services.pinconeservice import get_index

logger = logging.getLogger(__name__)

SERVICES_INDEX = "easybali-services"
BATCH_SIZE = 50          # Pinecone upsert batch size
EMBED_MODEL = "text-embedding-ada-002"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(val) -> str:
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none", "") else s


def _slug(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-z0-9]", "_", text.lower())[:max_len]


# ---------------------------------------------------------------------------
# Chunk builders
# ---------------------------------------------------------------------------

def _build_service_chunks(services_df) -> List[Dict[str, Any]]:
    chunks = []
    for _, row in services_df.drop_duplicates(subset=["Service Item"]).iterrows():
        name = _safe_str(row.get("Service Item", ""))
        if not name:
            continue

        category    = _safe_str(row.get("Category", ""))
        subcategory = _safe_str(row.get("Sub-category", ""))
        description = _safe_str(row.get("Service Item Description", ""))
        price       = _safe_str(row.get("Final Price (Service Item Button)", ""))

        parts = [f"EasyBali Service: {name}"]
        if category:
            parts.append(f"Category: {category}")
        if subcategory:
            parts.append(f"Type: {subcategory}")
        if description:
            parts.append(f"Description: {description}")
        if price:
            parts.append(f"Standard Price: IDR {price}")

        chunks.append({
            "id": f"svc_{_slug(name)}",
            "text": " | ".join(parts),
            "metadata": {
                "type": "service",
                "service_name": name,
                "category": category,
                "subcategory": subcategory,
                "price": price,
                "villa_code": "ALL",
            },
        })
    return chunks


def _build_pricing_chunks(price_df) -> List[Dict[str, Any]]:
    chunks = []
    fixed_cols = {"Service Item", "Vendor Price", "Villa Comm"}
    zone_cols = [
        c for c in price_df.columns
        if c not in fixed_cols and _safe_str(c)
    ]

    for _, row in price_df.iterrows():
        name = _safe_str(row.get("Service Item", ""))
        if not name:
            continue

        vendor       = _safe_str(row.get("Vendor Price", ""))
        generic_comm = _safe_str(row.get("Villa Comm", ""))
        base_id      = f"price_{_slug(name)}"

        # Generic split
        parts = [f"Pricing for {name}"]
        if vendor:
            parts.append(f"service provider earns IDR {vendor}")
        if generic_comm:
            parts.append(f"standard villa commission IDR {generic_comm}")
        chunks.append({
            "id": base_id,
            "text": " | ".join(parts),
            "metadata": {"type": "pricing", "service_name": name, "villa_code": "ALL"},
        })

        # Zone-specific splits
        for zone in zone_cols:
            zone_val = _safe_str(row.get(zone, ""))
            if not zone_val or zone_val == "0":
                continue
            zone_parts = [f"Pricing for {name} in {zone}"]
            zone_parts.append(f"villa commission IDR {zone_val}")
            if vendor:
                zone_parts.append(f"service provider earns IDR {vendor}")
            chunks.append({
                "id": f"{base_id}_{_slug(zone, 20)}",
                "text": " | ".join(zone_parts),
                "metadata": {
                    "type": "pricing_zone",
                    "service_name": name,
                    "location": zone,
                    "villa_code": "ALL",
                },
            })
    return chunks


def _build_villa_chunks(villas_df) -> List[Dict[str, Any]]:
    chunks = []
    for _, row in villas_df.iterrows():
        code     = _safe_str(row.get("Number", ""))
        name     = _safe_str(row.get("Name of Villa", ""))
        location = _safe_str(row.get("Location", ""))
        address  = _safe_str(row.get("Address", ""))
        if not code:
            continue

        parts = [f"Villa {name} (code {code})"]
        if location:
            parts.append(f"located in {location}")
        if address:
            parts.append(f"address: {address}")

        chunks.append({
            "id": f"villa_{_slug(code)}",
            "text": " | ".join(parts),
            "metadata": {
                "type": "villa",
                "villa_code": code,
                "villa_name": name,
                "location": location,
            },
        })
    return chunks


def build_services_chunks(cache: dict) -> List[Dict[str, Any]]:
    """Convert the full sheet cache into embeddable text chunks."""
    chunks: List[Dict[str, Any]] = []

    services_df = cache.get("services_df")
    if services_df is not None and not services_df.empty:
        chunks.extend(_build_service_chunks(services_df))

    price_df = cache.get("price_distribution")
    if price_df is not None and not price_df.empty:
        chunks.extend(_build_pricing_chunks(price_df))

    villas_df = cache.get("villas_data")
    if villas_df is not None and not villas_df.empty:
        chunks.extend(_build_villa_chunks(villas_df))

    logger.info(f"[EmbeddingService] Built {len(chunks)} chunks from sheet cache")
    return chunks


# ---------------------------------------------------------------------------
# Embed + upsert
# ---------------------------------------------------------------------------

async def embed_and_upsert_services(cache: dict) -> bool:
    """
    Embed all sheet chunks and upsert into Pinecone 'easybali-services'.
    Returns True on success. Silently returns False on failure (never raises).
    """
    try:
        index = get_index(SERVICES_INDEX)
        if not index:
            logger.warning("[EmbeddingService] Pinecone index unavailable — skipping")
            return False

        chunks = build_services_chunks(cache)
        if not chunks:
            logger.warning("[EmbeddingService] No chunks built — nothing to upsert")
            return False

        total = 0
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i : i + BATCH_SIZE]
            texts = [c["text"] for c in batch]

            embed_resp = await client.embeddings.create(
                input=texts,
                model=EMBED_MODEL,
            )

            upsert_payload = [
                {
                    "id": batch[j]["id"],
                    "values": embed_resp.data[j].embedding,
                    "metadata": {**batch[j]["metadata"], "text": batch[j]["text"]},
                }
                for j in range(len(batch))
            ]
            index.upsert(vectors=upsert_payload)
            total += len(batch)
            logger.info(f"[EmbeddingService] Upserted batch {i // BATCH_SIZE + 1} ({len(batch)} vectors)")

        logger.info(f"✅ [EmbeddingService] Complete — {total} chunks in '{SERVICES_INDEX}'")
        return True

    except Exception as e:
        logger.error(f"[EmbeddingService] Failed (non-blocking): {e}")
        return False
