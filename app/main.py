from __future__ import annotations
import time
import uuid
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.ingestion import ingest_events
from app.metrics import compute_metrics
from app.funnel import compute_funnel
from app.heatmap import compute_heatmap
from app.anomalies import detect_anomalies
from app.health import get_health
from app.logging_config import setup_logging
from app.dashboard import router as dashboard_router

setup_logging()
logger = logging.getLogger("store_intelligence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(
    title="Store Intelligence API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(dashboard_router)


# ── Middleware: trace-id + structured request logging ──────────────────────────

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id
    start = time.time()

    response = await call_next(request)

    latency_ms = round((time.time() - start) * 1000, 2)
    store_id = request.path_params.get("id", None)

    logger.info(
        "request",
        extra={
            "extra": {
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "store_id": store_id,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            }
        },
    )

    response.headers["X-Trace-Id"] = trace_id
    return response


# ── Exception handlers ─────────────────────────────────────────────────────────

def _error_body(code: str, message: str, trace_id: str = "") -> dict:
    return {"error": {"code": code, "message": message, "trace_id": trace_id}}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    trace_id = getattr(request.state, "trace_id", "")
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", str(exc.errors()), trace_id),
    )


@app.exception_handler(OperationalError)
async def db_error_handler(request: Request, exc: OperationalError):
    trace_id = getattr(request.state, "trace_id", "")
    logger.error("Database operational error", exc_info=True)
    return JSONResponse(
        status_code=503,
        content=_error_body("DATABASE_UNAVAILABLE", "Database is temporarily unavailable", trace_id),
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", "")
    logger.error("Unhandled exception", exc_info=True)
    return JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", "An internal error occurred", trace_id),
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/events/ingest")
async def ingest_endpoint(
    request: Request,
    db: Session = Depends(get_db),
):
    """Accept a batch of up to 500 events. Validates individually; partial success supported."""
    trace_id = getattr(request.state, "trace_id", "")
    try:
        body: Any = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=_error_body("INVALID_JSON", "Request body must be valid JSON", trace_id),
        )

    if not isinstance(body, dict) or "events" not in body:
        return JSONResponse(
            status_code=400,
            content=_error_body("INVALID_BODY", "Body must be {\"events\": [...]}", trace_id),
        )

    raw_events = body["events"]
    if not isinstance(raw_events, list):
        return JSONResponse(
            status_code=400,
            content=_error_body("INVALID_BODY", "\"events\" must be a list", trace_id),
        )

    if len(raw_events) > 500:
        return JSONResponse(
            status_code=400,
            content=_error_body("BATCH_TOO_LARGE", "Batch size must not exceed 500 events", trace_id),
        )

    result = ingest_events(raw_events, db)

    logger.info("ingest_complete", extra={"extra": {
        "trace_id": trace_id,
        "event_count": len(raw_events),
        "accepted": result.accepted,
        "duplicates": result.duplicates,
        "rejected": result.rejected,
    }})

    return result


@app.get("/stores/{id}/metrics")
def metrics_endpoint(id: str, db: Session = Depends(get_db)):
    return compute_metrics(id, db)


@app.get("/stores/{id}/funnel")
def funnel_endpoint(id: str, db: Session = Depends(get_db)):
    return compute_funnel(id, db)


@app.get("/stores/{id}/heatmap")
def heatmap_endpoint(id: str, db: Session = Depends(get_db)):
    return compute_heatmap(id, db)


@app.get("/stores/{id}/anomalies")
def anomalies_endpoint(id: str, db: Session = Depends(get_db)):
    return detect_anomalies(id, db)


@app.get("/health")
def health_endpoint(db: Session = Depends(get_db)):
    result, status_code = get_health(db)
    return JSONResponse(content=result.model_dump(mode="json"), status_code=status_code)
