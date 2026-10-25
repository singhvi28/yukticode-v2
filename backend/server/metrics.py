import logging
import time

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from server.db.database import async_session_maker

logger = logging.getLogger(__name__)

# Ingress RED Signals
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests received",
    ["method", "endpoint", "status_code"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Queue Saturation Metrics
JUDGE_QUEUE_DEPTH = Gauge(
    "judge_queue_depth",
    "Current number of jobs in PostgreSQL queue by status",
    ["status"],
)
JUDGE_SUBMISSIONS_TOTAL = Gauge(
    "judge_submissions_total",
    "Total submissions by terminal verdict",
    ["verdict"],
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/metrics", "/health"):
            return await call_next(request)

        start_time = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.monotonic() - start_time
            endpoint = request.url.path
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                endpoint=endpoint,
                status_code=status_code,
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=request.method,
                endpoint=endpoint,
            ).observe(duration)


async def update_queue_metrics():
    """Query Postgres to refresh judge_jobs and submission counts."""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                text("SELECT status, count(*) FROM judge_jobs GROUP BY status")
            )
            for s in ("queued", "running", "done", "failed"):
                JUDGE_QUEUE_DEPTH.labels(status=s).set(0)
            for row in result.all():
                JUDGE_QUEUE_DEPTH.labels(status=row[0]).set(row[1])

            sub_result = await session.execute(
                text("SELECT status, count(*) FROM submissions GROUP BY status")
            )
            for s in ("AC", "WA", "TLE", "MLE", "CE", "RE", "SYSTEM_ERROR", "PENDING"):
                JUDGE_SUBMISSIONS_TOTAL.labels(verdict=s).set(0)
            for row in sub_result.all():
                JUDGE_SUBMISSIONS_TOTAL.labels(verdict=row[0]).set(row[1])
    except Exception:
        logger.exception("Failed to update queue metrics from database")


async def metrics_endpoint():
    await update_queue_metrics()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
