import json
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.cache import CacheService
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.db.session import engine


app = FastAPI(
    title=settings.APP_NAME,
    version="2.0.0",
    description="Analytics platform for data ingestion, profiling, transformation, dashboards, and reports.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("dataflow.requests")


def _client_address(request: Request) -> str:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def security_and_rate_limit(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "")[:100] or str(uuid4())
    started_at = perf_counter()

    if request.url.path.endswith(("/auth/login", "/auth/register")):
        try:
            attempts = CacheService.increment(
                f"rate-limit:auth:{_client_address(request)}",
                settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
            )
        except RuntimeError:
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication protection is temporarily unavailable."},
            )
        if attempts > settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many authentication attempts. Try again shortly."},
            )

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path in {"/docs", "/redoc"}:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' https://cdn.jsdelivr.net; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"

    logger.info(
        json.dumps(
            {
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            }
        )
    )
    return response


register_exception_handlers(app)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health/live", tags=["Health"])
def liveness_check():
    return {"status": "ok"}


@app.get("/health/ready", tags=["Health"])
def readiness_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable", "cache": "unknown"},
        )

    try:
        cache_available = CacheService.ping()
    except RuntimeError:
        cache_available = False
    if settings.ENVIRONMENT.lower() not in {"development", "test"} and not cache_available:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "available", "cache": "unavailable"},
        )
    return {
        "status": "ready",
        "database": "available",
        "cache": "available" if cache_available else "fallback",
    }
