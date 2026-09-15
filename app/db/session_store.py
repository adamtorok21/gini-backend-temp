import asyncio
from datetime import datetime
from typing import Any, Dict, Optional
from pymongo import IndexModel, ASCENDING
from app.db.session import db

# The unified collections for all sessions
session_collection = db["user_sessions"]

class SessionManager:
    """
    Unified MongoDB session manager replacing in-memory dictionaries.
    Handles active_chat_sessions, order_sessions, local_order_store, website_sessions, and various bot states.
    Automatically handles expiration via MongoDB TTL index (24 hours).
    """
    
    @classmethod
    async def init_indexes(cls):
        # Create a TTL index that expires documents 24 hours after their last 'updated_at' update
        index = IndexModel([("updated_at", ASCENDING)], expireAfterSeconds=86400)
        await session_collection.create_indexes([index])

    @classmethod
    async def get_session(cls, session_id: str) -> Dict[str, Any]:
        doc = await session_collection.find_one({"_id": session_id})
        return doc if doc else {}

    @classmethod
    async def get_field(cls, session_id: str, field: str) -> Any:
        doc = await session_collection.find_one({"_id": session_id}, {field: 1})
        return doc.get(field) if doc else None

    @classmethod
    async def set_field(cls, session_id: str, field: str, value: Any):
        await session_collection.update_one(
            {"_id": session_id},
            {"$set": {field: value, "updated_at": datetime.utcnow()}},
            upsert=True
        )

    @classmethod
    async def delete_field(cls, session_id: str, field: str):
        await session_collection.update_one(
            {"_id": session_id},
            {"$unset": {field: ""}, "$set": {"updated_at": datetime.utcnow()}}
        )

    @classmethod
    async def delete_session(cls, session_id: str):
        await session_collection.delete_one({"_id": session_id})

    @classmethod
    async def get_all_by_field(cls, field: str) -> Dict[str, Any]:
        """Returns all session IDs and their values for a specific field"""
        cursor = session_collection.find({field: {"$exists": True}}, {"_id": 1, field: 1})
        docs = await cursor.to_list(length=None)
        return {str(doc["_id"]): doc[field] for doc in docs}

    @classmethod
    async def heartbeat(cls, session_id: str):
        """Updates the updated_at timestamp to prevent expiration"""
        await session_collection.update_one(
            {"_id": session_id},
            {"$set": {"updated_at": datetime.utcnow()}},
            upsert=True
        )

session_manager = SessionManager()
