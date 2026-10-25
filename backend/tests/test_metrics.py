from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from starlette.testclient import TestClient

from server.main import app
from server.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    JUDGE_QUEUE_DEPTH,
    JUDGE_SUBMISSIONS_TOTAL,
    update_queue_metrics,
)
from worker.metrics import (
    WORKER_ACTIVE_JOBS,
    WORKER_JOB_DURATION_SECONDS,
    WORKER_JOBS_TOTAL,
    WORKER_REAPED_CONTAINERS_TOTAL,
    WORKER_REQUEUED_LEASES_TOTAL,
)
from worker.main import process_job


def test_metrics_endpoint_returns_prometheus_format():
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")

    body = response.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    assert "judge_queue_depth" in body
    assert "judge_submissions_total" in body


def test_prometheus_middleware_tracks_non_ignored_requests():
    client = TestClient(app)

    # Request /docs which does not require DB access
    initial = HTTP_REQUESTS_TOTAL.labels(
        method="GET", endpoint="/docs", status_code=200
    )._value.get()

    res = client.get("/docs")
    assert res.status_code == 200

    updated = HTTP_REQUESTS_TOTAL.labels(
        method="GET", endpoint="/docs", status_code=200
    )._value.get()
    assert updated == initial + 1


def test_prometheus_middleware_skips_health_and_metrics():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_maker = MagicMock()
    mock_maker.return_value.__aenter__.return_value = mock_session
    mock_maker.return_value.__aexit__.return_value = None

    with patch("server.main.async_session_maker", mock_maker), \
         patch("server.metrics.async_session_maker", mock_maker):
        client = TestClient(app)
        res_health = client.get("/health")
        assert res_health.status_code == 200

        res_metrics = client.get("/metrics")
        assert res_metrics.status_code == 200

    # Ensure neither /health nor /metrics is tracked in HTTP_REQUESTS_TOTAL
    for metric in HTTP_REQUESTS_TOTAL.collect():
        for s in metric.samples:
            assert s.labels.get("endpoint") not in ("/metrics", "/health")


@pytest.mark.asyncio
async def test_update_queue_metrics_sets_gauges():
    mock_session = AsyncMock()
    mock_jobs_result = MagicMock()
    mock_jobs_result.all.return_value = [("queued", 7), ("running", 2), ("failed", 1)]

    mock_subs_result = MagicMock()
    mock_subs_result.all.return_value = [("AC", 15), ("WA", 5)]

    mock_session.execute = AsyncMock(side_effect=[mock_jobs_result, mock_subs_result])

    mock_maker = MagicMock()
    mock_maker.return_value.__aenter__.return_value = mock_session
    mock_maker.return_value.__aexit__.return_value = None

    with patch("server.metrics.async_session_maker", mock_maker):
        await update_queue_metrics()

    assert JUDGE_QUEUE_DEPTH.labels(status="queued")._value.get() == 7
    assert JUDGE_QUEUE_DEPTH.labels(status="running")._value.get() == 2
    assert JUDGE_QUEUE_DEPTH.labels(status="failed")._value.get() == 1
    assert JUDGE_QUEUE_DEPTH.labels(status="done")._value.get() == 0

    assert JUDGE_SUBMISSIONS_TOTAL.labels(verdict="AC")._value.get() == 15
    assert JUDGE_SUBMISSIONS_TOTAL.labels(verdict="WA")._value.get() == 5
    assert JUDGE_SUBMISSIONS_TOTAL.labels(verdict="TLE")._value.get() == 0


@pytest.mark.asyncio
async def test_update_queue_metrics_handles_db_failure_gracefully():
    mock_maker = MagicMock()
    mock_maker.return_value.__aenter__.side_effect = Exception("Database connection error")

    with patch("server.metrics.async_session_maker", mock_maker):
        # Should catch and log, not raise
        await update_queue_metrics()

    with patch("server.metrics.async_session_maker", mock_maker):
        client = TestClient(app)
        res = client.get("/metrics")
        assert res.status_code == 200


def test_worker_metrics_definitions():
    initial_leases = WORKER_REQUEUED_LEASES_TOTAL._value.get()
    WORKER_REQUEUED_LEASES_TOTAL.inc(3)
    assert WORKER_REQUEUED_LEASES_TOTAL._value.get() == initial_leases + 3

    initial_reaped = WORKER_REAPED_CONTAINERS_TOTAL._value.get()
    WORKER_REAPED_CONTAINERS_TOTAL.inc(2)
    assert WORKER_REAPED_CONTAINERS_TOTAL._value.get() == initial_reaped + 2


@pytest.mark.asyncio
async def test_worker_process_job_telemetry_submit_success():
    job = {
        "id": "test-job-1",
        "kind": "submit",
        "payload": {"submission_id": 10},
        "submission_id": 10,
    }

    mock_db = AsyncMock()
    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db
    mock_session_maker.return_value.__aexit__.return_value = None

    initial_ac = WORKER_JOBS_TOTAL.labels(kind="submit", status="AC")._value.get()

    with patch("worker.main.create_async_engine") as mock_engine, \
         patch("worker.main.async_sessionmaker", return_value=mock_session_maker), \
         patch("worker.main.fetch_submission_data", return_value={
             "language": "py", "source": "print(1)", "test_cases": [],
             "time_limit_ms": 1000, "memory_limit_mb": 256
         }), \
         patch("worker.judger.run_submit", return_value={"status": "AC", "execution_time_ms": 10.0}), \
         patch("worker.main.complete_job", new_callable=AsyncMock):
        
        mock_engine.return_value.dispose = AsyncMock()
        await process_job(job, "test-worker")

    assert WORKER_JOBS_TOTAL.labels(kind="submit", status="AC")._value.get() == initial_ac + 1
    assert WORKER_ACTIVE_JOBS._value.get() == 0


@pytest.mark.asyncio
async def test_worker_process_job_telemetry_run_success():
    job = {
        "id": "test-job-run",
        "kind": "run",
        "payload": {
            "language": "py",
            "src_code": "print(42)",
            "std_in": "",
            "expected_output": "42",
            "time_limit": 1000,
            "memory_limit": 256,
        },
    }

    mock_db = AsyncMock()
    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db
    mock_session_maker.return_value.__aexit__.return_value = None

    initial_ac = WORKER_JOBS_TOTAL.labels(kind="run", status="AC")._value.get()

    with patch("worker.main.create_async_engine") as mock_engine, \
         patch("worker.main.async_sessionmaker", return_value=mock_session_maker), \
         patch("worker.judger.run_custom", return_value={"status": "AC"}), \
         patch("worker.main.complete_job", new_callable=AsyncMock):

        mock_engine.return_value.dispose = AsyncMock()
        await process_job(job, "test-worker")

    assert WORKER_JOBS_TOTAL.labels(kind="run", status="AC")._value.get() == initial_ac + 1
    assert WORKER_ACTIVE_JOBS._value.get() == 0


@pytest.mark.asyncio
async def test_worker_process_job_telemetry_failure():
    job = {
        "id": "test-job-2",
        "kind": "unknown_action",
        "payload": {},
    }

    mock_db = AsyncMock()
    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db
    mock_session_maker.return_value.__aexit__.return_value = None

    initial_failed = WORKER_JOBS_TOTAL.labels(kind="unknown_action", status="failed")._value.get()

    with patch("worker.main.create_async_engine") as mock_engine, \
         patch("worker.main.async_sessionmaker", return_value=mock_session_maker), \
         patch("worker.main.fail_job", new_callable=AsyncMock):

        mock_engine.return_value.dispose = AsyncMock()
        await process_job(job, "test-worker")

    assert WORKER_JOBS_TOTAL.labels(kind="unknown_action", status="failed")._value.get() == initial_failed + 1
    assert WORKER_ACTIVE_JOBS._value.get() == 0


@pytest.mark.asyncio
async def test_worker_process_job_telemetry_run_batch_success():
    job = {
        "id": "test-job-batch",
        "kind": "run_batch",
        "payload": {
            "language": "py",
            "src_code": "print(1)",
            "tests": [{"input": "1"}],
            "time_limit": 1000,
            "memory_limit": 256,
        },
    }

    mock_db = AsyncMock()
    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db
    mock_session_maker.return_value.__aexit__.return_value = None

    initial_done = WORKER_JOBS_TOTAL.labels(kind="run_batch", status="done")._value.get()

    with patch("worker.main.create_async_engine") as mock_engine, \
         patch("worker.main.async_sessionmaker", return_value=mock_session_maker), \
         patch("worker.judger.run_batch", return_value={"tests": [{"test_index": 0, "status": "AC"}]}), \
         patch("worker.main.complete_job", new_callable=AsyncMock):

        mock_engine.return_value.dispose = AsyncMock()
        await process_job(job, "test-worker")

    assert WORKER_JOBS_TOTAL.labels(kind="run_batch", status="done")._value.get() == initial_done + 1
    assert WORKER_ACTIVE_JOBS._value.get() == 0


@pytest.mark.asyncio
async def test_worker_run_requeue_and_reap_telemetry():
    import asyncio
    from worker.main import run

    mock_db = AsyncMock()
    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db
    mock_session_maker.return_value.__aexit__.return_value = None

    initial_requeued = WORKER_REQUEUED_LEASES_TOTAL._value.get()
    initial_reaped = WORKER_REAPED_CONTAINERS_TOTAL._value.get()

    # We mock time.monotonic so the reap check triggers:
    # time.monotonic() - last_reap > REAP_INTERVAL_SECONDS
    time_values = [0.0, 1000.0, 1000.0, 1000.0]

    with patch("worker.main.start_http_server"), \
         patch("worker.judger.ensure_image"), \
         patch("worker.main.create_async_engine") as mock_engine, \
         patch("worker.main.async_sessionmaker", return_value=mock_session_maker), \
         patch("worker.main.requeue_expired_jobs", return_value=[{"id": "j1", "requeued": True}, {"id": "j2", "requeued": True}]), \
         patch("worker.main.claim_job", return_value=None), \
         patch("time.monotonic", side_effect=lambda: time_values.pop(0) if time_values else 2000.0), \
         patch("worker.judger.reap_orphaned_containers", return_value=4), \
         patch("asyncio.sleep", side_effect=asyncio.CancelledError):

        mock_engine.return_value.dispose = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await run()

    assert WORKER_REQUEUED_LEASES_TOTAL._value.get() == initial_requeued + 2
    assert WORKER_REAPED_CONTAINERS_TOTAL._value.get() == initial_reaped + 4

