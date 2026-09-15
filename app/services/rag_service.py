import logging
import pandas as pd
from typing import List, Optional, Dict, Any
from app.services.pinconeservice import get_index
from app.services.openai_client import client
from app.settings.config import settings

logger = logging.getLogger(__name__)

class RAGService:
    @staticmethod
    async def get_rag_context(query: str, chat_type: str = "general", villa_code: str = "WEB_VILLA_01") -> str:
        """
        Unified RAG retrieval logic for both Web and WhatsApp.
        Intelligently selects the best Pinecone index based on query and chat_type.
        """
        try:
            # 1. Search all EXISTING curated indexes (small, so querying all is cheap and
            #    avoids brittle keyword routing). Missing indexes are skipped by get_index.
            indexes_to_search = [
                ("things-to-do-in-bali", None),
                ("local-cuisine", None),
                ("ai-data", None),
                ("event-calender", None),
                ("language-lesson-sheet", None),
            ]

            # De-duplicate while preserving order (some might have matched multiple keywords)
            seen = set()
            unique_indexes = []
            for idx, filt in indexes_to_search:
                if idx not in seen:
                    unique_indexes.append((idx, filt))
                    seen.add(idx)

            # Generate embedding once
            embed_resp = await client.embeddings.create(input=query, model="text-embedding-ada-002")
            query_vector = embed_resp.data[0].embedding

            all_context_pieces = []
            
            for index_name, filter_dict in unique_indexes:
                index = get_index(index_name)
                if not index:
                    continue
                
                res = index.query(
                    vector=query_vector,
                    top_k=3,
                    include_metadata=True,
                    filter=filter_dict
                )
                
                matches = res.get("matches", [])
                # Use a lower threshold for villa-faqs (admin-curated Q&A pairs are trusted)
                threshold = 0.60 if index_name == "villa-faqs" else 0.70
                for m in matches:
                    score = m.get("score", 0)
                    text = m.get("metadata", {}).get("text", "").strip()
                    if text and score > threshold:
                        source_label = index_name.replace("-", " ").title()
                        all_context_pieces.append(f"[{source_label}]: {text}")

            if not all_context_pieces:
                logger.info(f"RAG MISS for query: '{query[:50]}...' across {seen}")
                return ""

            final_context = "\n\n".join(all_context_pieces)
            logger.info(f"RAG HIT: Found {len(all_context_pieces)} pieces for query: '{query[:50]}...'")
            return final_context[:8000] # Token safety cap

        except Exception as e:
            if "403" in str(e) or "Forbidden" in str(e):
                logger.error(f"❌ Pinecone Quota Exceeded (403): {e}. RAG falling back to local context.")
            else:
                logger.error(f"Unified RAG Service Error: {e}")
            return ""

rag_service = RAGService()
