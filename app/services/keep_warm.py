"""
Keep-warm pinger — prevents the Render instance from sleeping.

Why this exists (2026-07-22):
Meta periodically health-check-pings each WhatsApp Flow's data-exchange endpoint
({settings.BASE_URL}/category-flow, /venue-setup-flow). Render's instance sleeps
after ~15 min with no inbound traffic; the next request (including Meta's ping)
then pays a 30-60s cold start, which exceeds Meta's few-second endpoint timeout.
Repeated ping timeouts make Meta flag the flow endpoint as unhealthy and show
guests "Sorry, this content isn't available right now" — even though the endpoint,
crypto, and screens are all correct.

Fix: self-ping the app's own public URL every 10 minutes (< the sleep threshold),
so the instance never sleeps and Meta's pings always land on a warm endpoint.

Non-fatal by design: a failed ping is logged and the loop continues.
"""
import asyncio
import logging

import httpx

from app.settings.config import settings

logger = logging.getLogger(__name__)

# 10 min — comfortably under Render's ~15 min inactivity sleep threshold.
_INTERVAL_SECONDS = 600


async def start_keep_warm():
    base = (getattr(settings, "BASE_URL", "") or "").rstrip("/")
    if not base.startswith("http"):
        logger.warning(f"⏰ keep-warm disabled — BASE_URL not a URL: {base!r}")
        return

    url = f"{base}/"
    logger.info(f"⏰ keep-warm started — pinging {url} every {_INTERVAL_SECONDS}s")
    # small initial delay so it never races startup
    await asyncio.sleep(_INTERVAL_SECONDS)
    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url)
            logger.info(f"⏰ keep-warm ping -> {r.status_code}")
        except Exception as e:
            logger.warning(f"⏰ keep-warm ping failed (non-fatal): {e}")
        await asyncio.sleep(_INTERVAL_SECONDS)
