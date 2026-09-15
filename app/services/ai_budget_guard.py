"""
AI Budget Guard — application-side daily usage cap and emergency kill switch.

This runs BEFORE every OpenAI call (chat, Whisper, TTS, embeddings) and blocks
the call if any of the following are true:

  1. AI_ENABLED=false               → kill switch (immediate block, all AI)
  2. AI_EMERGENCY_KILL_SWITCH=true  → same as above, alias
  3. Daily request count ≥ AI_DAILY_REQUEST_LIMIT
  4. Daily input-token estimate ≥ AI_DAILY_TOKEN_LIMIT

Counters live in MongoDB (ai_usage_daily collection) keyed by UTC date string
("2026-06-21"). They reset automatically because the key changes each day.

Usage:
    from app.services.ai_budget_guard import ai_budget_guard, BudgetExceeded

    async def my_openai_caller():
        await ai_budget_guard.check("chat")   # raises BudgetExceeded if blocked
        # ... make the OpenAI call ...
        await ai_budget_guard.record("chat", input_tokens=512, output_tokens=120)

Both check() and record() are non-fatal on DB errors — a Mongo hiccup never
blocks a real guest. The guard fails OPEN (like the rate limiter).
"""

import logging
from datetime import datetime, UTC
from typing import Literal

from app.settings.config import settings

logger = logging.getLogger(__name__)

# ── Sentinel returned by check() when the guard is disabled or DB fails ──────
_OPEN = "open"   # means: proceed, no block


class BudgetExceeded(Exception):
    """Raised by check() when a daily cap or kill switch blocks the call."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


CallType = Literal["chat", "audio", "tts", "embedding"]

# Cost-per-1K-token estimates used only for log annotation (not for blocking).
# Blocking uses request count and raw token count, not cost, to stay simple.
_COST_PER_1K = {
    "gpt-4o":                0.0025,   # input
    "gpt-4o-mini":           0.00015,
    "text-embedding-ada-002":0.00010,
    "whisper-1":             0.00600,  # per minute, approximated per request
    "tts-1":                 0.01500,  # per 1K chars, approximated
}


class AIBudgetGuard:
    """
    Singleton guard.  Import the module-level instance `ai_budget_guard`.
    """

    def _today_key(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _is_killed(self) -> bool:
        """True when the kill switch is active (either env var form)."""
        if not settings.AI_ENABLED:
            return True
        if settings.AI_EMERGENCY_KILL_SWITCH:
            return True
        return False

    async def check(self, call_type: CallType, *, estimated_input_chars: int = 0) -> None:
        """
        Call this BEFORE any OpenAI API call.

        Raises BudgetExceeded if:
          - kill switch is active
          - daily request limit reached
          - daily token limit reached (based on estimated_input_chars / 4 tokens/char)

        Fails OPEN on any DB error — never blocks a real guest due to a Mongo hiccup.
        """
        # ── Kill switch ───────────────────────────────────────────────────────
        if self._is_killed():
            logger.warning("[ai-budget] KILL SWITCH active — OpenAI call blocked.")
            raise BudgetExceeded("AI is currently disabled. Please try again later.")

        # ── Daily caps ────────────────────────────────────────────────────────
        req_limit   = settings.AI_DAILY_REQUEST_LIMIT
        token_limit = settings.AI_DAILY_TOKEN_LIMIT

        if req_limit <= 0 and token_limit <= 0:
            return  # caps disabled, nothing to check

        try:
            from app.db.session import ai_usage_collection
            today = self._today_key()
            doc = await ai_usage_collection.find_one({"_id": today})

            if doc:
                if req_limit > 0 and doc.get("requests", 0) >= req_limit:
                    logger.warning(
                        f"[ai-budget] Daily request cap ({req_limit}) reached "
                        f"(current={doc['requests']}). Blocking {call_type} call."
                    )
                    raise BudgetExceeded(
                        "We're currently experiencing high demand. "
                        "Please try again in a few minutes."
                    )

                estimated_tokens = estimated_input_chars // 4
                if token_limit > 0 and doc.get("input_tokens", 0) + estimated_tokens >= token_limit:
                    logger.warning(
                        f"[ai-budget] Daily token cap ({token_limit}) reached "
                        f"(current={doc['input_tokens']}). Blocking {call_type} call."
                    )
                    raise BudgetExceeded(
                        "We're currently experiencing high demand. "
                        "Please try again in a few minutes."
                    )
        except BudgetExceeded:
            raise
        except Exception as _e:
            # Fail open — DB error must never block guests
            logger.warning(f"[ai-budget] check() DB error (fail-open): {_e}")

    async def record(
        self,
        call_type: CallType,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
        villa_code: str = "",
        user_id: str = "",
        ip: str = "",
        blocked: bool = False,
        block_reason: str = "",
    ) -> None:
        """
        Record one AI call attempt (allowed or blocked) to ai_usage_daily.

        Non-fatal on DB error.
        """
        try:
            from app.db.session import ai_usage_collection
            today = self._today_key()
            inc = {
                "requests": 1,
                f"requests_{call_type}": 1,
            }
            if blocked:
                inc["blocked"] = 1
            else:
                inc["input_tokens"]  = input_tokens
                inc["output_tokens"] = output_tokens

            await ai_usage_collection.update_one(
                {"_id": today},
                {
                    "$inc": inc,
                    "$set": {"date": today},
                    "$push": {
                        "recent": {
                            "$each": [{
                                "ts":           datetime.now(UTC).isoformat(),
                                "type":         call_type,
                                "model":        model,
                                "villa_code":   villa_code,
                                "user_id":      user_id[:32] if user_id else "",
                                "ip":           ip,
                                "input_tokens": input_tokens,
                                "output_tokens":output_tokens,
                                "blocked":      blocked,
                                "block_reason": block_reason,
                            }],
                            "$slice": -200,   # keep last 200 events per day
                        }
                    },
                },
                upsert=True,
            )
        except Exception as _e:
            logger.warning(f"[ai-budget] record() DB error (non-fatal): {_e}")

    async def today_summary(self) -> dict:
        """Return today's usage stats. Used by the /health/ai-usage admin endpoint."""
        try:
            from app.db.session import ai_usage_collection
            doc = await ai_usage_collection.find_one({"_id": self._today_key()})
            if not doc:
                return {"date": self._today_key(), "requests": 0, "input_tokens": 0,
                        "blocked": 0, "kill_switch": self._is_killed()}
            doc.pop("_id", None)
            doc.pop("recent", None)
            doc["kill_switch"]   = self._is_killed()
            doc["request_limit"] = settings.AI_DAILY_REQUEST_LIMIT
            doc["token_limit"]   = settings.AI_DAILY_TOKEN_LIMIT
            pct = 0
            if settings.AI_DAILY_REQUEST_LIMIT > 0:
                pct = round(doc.get("requests", 0) / settings.AI_DAILY_REQUEST_LIMIT * 100, 1)
            doc["request_limit_pct"] = pct
            return doc
        except Exception as _e:
            logger.warning(f"[ai-budget] today_summary() DB error: {_e}")
            return {"error": str(_e)}


# Module-level singleton — import this everywhere
ai_budget_guard = AIBudgetGuard()
