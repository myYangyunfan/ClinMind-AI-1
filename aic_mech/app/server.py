"""ClinMind-AI FastAPI Application Server.

Mounts static UI assets, API routing endpoints, WebSocket telemetry feeds,
and configures CORS and error handling.
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager, suppress
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import config

from app.api.routes_case import router as case_router
from app.api.routes_action import router as action_router
from app.api.routes_eval import router as eval_router
from app.api.routes_config import router as config_router
from app.api.websocket_feed import router as ws_router
from app.core.session_manager import SessionStore


@asynccontextmanager
async def lifespan(app):
    store = SessionStore.get_instance()
    async def clock():
        while True:
            await asyncio.sleep(1)
            store.tick_all()
    task = asyncio.create_task(clock())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for session in store.get_all_sessions().values():
            store.persist(session)


def create_app() -> FastAPI:
    """Create and configure the FastAPI web application."""
    app = FastAPI(
        title="ClinMind-AI: 临床决策思维推演与OSCE智能评测系统",
        description="本地临床教学模拟：1Hz生命体征仿真与OSCE思维复盘",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS Middleware: UI 与 API 同源部署且不使用 cookie 凭证；
    # allow_origins=["*"] 与 allow_credentials=True 的组合违反 CORS 规范。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API Routers
    app.include_router(case_router)
    app.include_router(action_router)
    app.include_router(eval_router)
    app.include_router(config_router)
    app.include_router(ws_router)

    # Ensure static directory exists
    config.STATIC_DIR.mkdir(parents=True, exist_ok=True)

    # Mount static assets directory
    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")

    @app.get("/")
    async def root():
        """Redirect home route to the emergency room dashboard."""
        return RedirectResponse(url="/static/reception.html")

    @app.get("/api/health", tags=["System"])
    async def health():
        return {"status": "ok", "version": "2.0.0", "mode": "online" if config.is_online_llm_active() else "offline", "storage": "sqlite"}

    return app


app = create_app()
