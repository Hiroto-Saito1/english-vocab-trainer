"""Assemble the HTTP application from its route and infrastructure components."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from english_vocab_trainer.adapters.local.vocabulary import SQLiteVocabularyRepository
from english_vocab_trainer.web.container import AppContainer, container_from_env
from english_vocab_trainer.web.routes.audio import router as audio_router
from english_vocab_trainer.web.routes.session import router as session_router
from english_vocab_trainer.web.routes.study import router as study_router
from english_vocab_trainer.web.routes.system import router as system_router

ROOT = Path(__file__).parent


def create_app(container: AppContainer) -> FastAPI:
    new_app = FastAPI(title="English Vocab Trainer", version="1.0.0")
    new_app.state.container = container
    if container.environment == "production":
        # A manually assembled production container is useful in tests; the
        # env factory never permits this fallback for an internet-facing app.
        allowed_hosts = list(container.trusted_hosts or ("localhost", "127.0.0.1", "testserver"))
        new_app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @new_app.middleware("http")
    async def private_response_cache_control(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if (
            (path.startswith("/api/") and not path.startswith("/api/v1/audio/"))
            or path.startswith("/auth/")
            or path == "/login"
        ):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self'; media-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'"
        )
        if container.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    new_app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    new_app.include_router(session_router)
    new_app.include_router(system_router)
    new_app.include_router(study_router)
    new_app.include_router(audio_router)
    return new_app


def create_app_from_env() -> FastAPI:
    container = container_from_env()
    with SQLiteVocabularyRepository(container.database):
        pass
    return create_app(container)
