import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from buma.gateway.health import status as health_status
from buma.gateway.routes.api_auth import router as api_auth_router
from buma.gateway.routes.auth import router as auth_router
from buma.gateway.routes.chat import router as chat_router
from buma.gateway.routes.config import router as config_router
from buma.gateway.routes.dev import router as dev_router
from buma.gateway.routes.observability import router as observability_router
from buma.gateway.routes.webhook import router as webhook_router


def create_app() -> FastAPI:
    app = FastAPI(title="Buma Gateway", version="0.1.0")

    cors_raw = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
    origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict:
        return health_status()

    app.include_router(api_auth_router)
    app.include_router(auth_router)
    app.include_router(webhook_router)
    app.include_router(config_router)
    app.include_router(observability_router)
    app.include_router(chat_router)
    app.include_router(dev_router)

    return app


app = create_app()
