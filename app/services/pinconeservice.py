import os
from typing import Any
from pinecone import Pinecone, ServerlessSpec
from app.settings.config import settings

def get_index(index_name: str, dimension: int = 1536, metric: str = "cosine") -> Any:
    """Safely get or create a Pinecone index."""
    try:
        pinecone_api_key = settings.pinecone_api_key
        pinecone_region = settings.pinecone_region
        pinecone_cloud = settings.pinecone_cloud

        if not pinecone_api_key:
            print("⚠️ PINECONE_API_KEY not found in settings")
            return None

        pc = Pinecone(api_key=pinecone_api_key)

        existing_indexes = [idx.name for idx in pc.list_indexes()]
        if index_name not in existing_indexes:
            # Do NOT auto-create (free tier is capped at 5 indexes -> 403). Skip gracefully.
            return None

        return pc.Index(index_name)
    except Exception as e:
        print(f"❌ Pinecone Error: {e}")
        return None
