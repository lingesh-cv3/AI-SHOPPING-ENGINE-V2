"""Engine API entrypoint.

Run with:
    python -m uvicorn engine.api.main:app --port 8000

Requires the sample merchant on port 8001, since the development connection points
at it.
"""

from .chat import router as chat_router
from .routes import app
from .shop import router as shop_router

app.include_router(shop_router)
app.include_router(chat_router)

__all__ = ["app"]