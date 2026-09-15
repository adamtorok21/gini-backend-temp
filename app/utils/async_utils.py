import asyncio
import logging

logger = logging.getLogger(__name__)

def safe_create_task(coro):
    """
    Safely creates a background task if an event loop is running and not closed.
    Prevents 'RuntimeError: Event loop is closed' during shutdown or tests.
    """
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            return loop.create_task(coro)
    except RuntimeError:
        # No running event loop
        pass
    except Exception as e:
        logger.error(f"Failed to create safe task: {e}")
    
    # If we couldn't create a task, we must close the coroutine to avoid warnings
    if asyncio.iscoroutine(coro):
        coro.close()
    return None
