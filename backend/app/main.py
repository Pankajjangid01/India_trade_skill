"""Backend entrypoint for Indian Day Trader UI - FastAPI + Socket.IO."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import socketio

from app import config as settings
from app.skillpack.runner import runner as skill_runner, auto_trader

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
auto_trader.sio = sio


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Indian Day Trader UI backend...")
    await auto_trader.start()
    yield
    logger.info("Shutting down...")
    await auto_trader.stop()


fastapi_app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
)

app = socketio.ASGIApp(sio, fastapi_app, socketio_path="/ws")

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Socket.IO events ----

@sio.event
async def connect(sid, environ):
    logger.info(f"UI client connected: {sid}")
    await sio.emit("state", auto_trader.get_state(), room=sid)


@sio.event
async def disconnect(sid):
    logger.info(f"UI client disconnected: {sid}")


# ---- API routes included below ----

def register_routes():
    from app.api.v1 import skillpack
    fastapi_app.include_router(skillpack.router, prefix="")


register_routes()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
