"""
OpenAI client factory.

Production path: returns AsyncOpenAI using OPENAI_API_KEY.
Test/CI path:    returns a no-op stub that raises RuntimeError on any real call,
                 ensuring no test can accidentally spend OpenAI credits.

A call is classified as "test/CI" when ANY of these are true:
  - PYTEST_CURRENT_TEST env var is set  (pytest sets this automatically)
  - ENV=test or ENV=ci                  (explicit test environment flag)
  - PLAYWRIGHT=true                     (Playwright test runner)

NOTE: CI=true is intentionally excluded. Render (our production host) sets CI=true
in its runtime environment. Checking CI would activate the no-op stub in production
and silently break all AI responses for real users.
"""

import os
import logging
from app.settings.config import settings
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def _is_test_env() -> bool:
    """Return True when running inside a local test/Playwright environment.

    Deliberately does NOT check CI=true — Render sets that in production runtime.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    env = os.environ.get("ENV", "").lower()
    if env in ("test", "ci"):
        return True
    if os.environ.get("PLAYWRIGHT", "").lower() in ("true", "1", "yes"):
        return True
    return False


class _NoOpOpenAI:
    """
    Stub client returned in test/CI environments.

    Any attribute access returns a nested stub.  Any actual *call* (e.g.
    client.chat.completions.create(...)) raises RuntimeError so the failure
    is loud and traceable rather than silently spending credits.
    """

    class _Stub:
        def __getattr__(self, name):
            return _NoOpOpenAI._Stub()

        async def __call__(self, *args, **kwargs):
            raise RuntimeError(
                "[openai-stub] Real OpenAI call attempted in test/CI environment. "
                "Use user_id prefixed 'test_' or 'sim_', or set "
                "ALLOW_REAL_OPENAI_IN_TESTS=true to opt into a real call "
                "(requires a separate test API key)."
            )

        def __await__(self):
            raise RuntimeError(
                "[openai-stub] Real OpenAI call attempted in test/CI environment."
            )

    def __getattr__(self, name):
        return self._Stub()


# ── Client construction ───────────────────────────────────────────────────────

_real_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

# Separate client for test/CI senders — only active when OPENAI_API_KEY_TEST is set.
_test_client = (
    AsyncOpenAI(api_key=settings.OPENAI_API_KEY_TEST)
    if settings.OPENAI_API_KEY_TEST
    else _real_client
)

# The module-level `client` used by ai_prompt.py and whatsapp_ai_prompt.py
# (direct imports of `from app.services.openai_client import client`).
# In test/CI environments and ALLOW_REAL_OPENAI_IN_TESTS=false, this is the
# no-op stub — real calls raise immediately with a clear error message.
if _is_test_env() and not settings.ALLOW_REAL_OPENAI_IN_TESTS:
    client = _NoOpOpenAI()
    logger.info("[openai-client] Test/CI environment detected — no-op stub active. "
                "Set ALLOW_REAL_OPENAI_IN_TESTS=true to enable real calls.")
else:
    client = _real_client


def get_client(user_id: str = "") -> AsyncOpenAI:
    """Return the appropriate OpenAI client for this user_id.

    - test_/sim_ prefixed senders → test client (separate billing bucket)
    - all others → production client
    - test/CI environment → no-op stub (regardless of user_id)
    """
    if _is_test_env() and not settings.ALLOW_REAL_OPENAI_IN_TESTS:
        return client  # already the no-op stub
    if user_id and user_id.lower().startswith(("test_", "sim_")):
        return _test_client
    return _real_client
