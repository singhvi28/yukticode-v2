"""Postgres-backed job queue.

The whole point of this module is that there is no broker. A worker claims one
row with `FOR UPDATE SKIP LOCKED`, does the work, writes the result, and
commits. If the worker dies, the lease expires and the job is claimable again.
"""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import JOB_MAX_ATTEMPTS


def _json_safe(value):
    """Postgres text/json cannot store NUL bytes; strip them recursively."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


async def claim_job(
    db: AsyncSession, worker_id: str, lease_seconds: int
) -> dict | None:
    result = await db.execute(
        text(
            """
            WITH next AS (
                SELECT id
                FROM judge_jobs
                WHERE status = 'queued'
                  AND available_at <= now()
                ORDER BY priority, available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE judge_jobs AS j
            SET status = 'running',
                locked_by = :worker_id,
                locked_until = now() + make_interval(secs => :lease_seconds),
                attempts = j.attempts + 1,
                updated_at = now()
            FROM next
            WHERE j.id = next.id
            RETURNING j.*
            """
        ),
        {"worker_id": worker_id, "lease_seconds": lease_seconds},
    )
    row = result.mappings().first()
    await db.commit()
    return dict(row) if row else None


async def complete_job(
    db: AsyncSession,
    job_id: str,
    result: dict,
    *,
    worker_id: str,
    submission_id: int | None = None,
) -> None:
    """Complete a job and, for submissions, persist the verdict atomically."""
    updated = await db.execute(
        text(
            """
            UPDATE judge_jobs
            SET status = 'done',
                result = CAST(:result AS json),
                locked_by = NULL,
                locked_until = NULL,
                updated_at = now()
            WHERE id = :job_id
              AND status = 'running'
              AND locked_by = :worker_id
            """
        ),
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "result": json.dumps(_json_safe(result)),
        },
    )
    if not updated.rowcount:
        # Another worker already completed or re-queued this lease. Never let
        # a duplicate completion rewrite a terminal verdict.
        await db.rollback()
        return

    if submission_id is not None:
        status = str(result.get("status", "SYSTEM_ERROR"))
        await db.execute(
            text(
                """
                UPDATE submissions
                SET status = :status,
                    execution_time_ms = :time_ms,
                    peak_memory_mb = :mem_mb,
                    message = :message
                WHERE id = :submission_id
                  AND status = 'PENDING'
                """
            ),
            {
                "submission_id": submission_id,
                "status": status,
                "time_ms": float(result.get("execution_time_ms", 0.0) or 0.0),
                "mem_mb": float(result.get("peak_memory_mb", 0.0) or 0.0),
                "message": (result.get("message") or "").replace("\x00", "")[:4000],
            },
        )

    await db.commit()


async def fail_job(
    db: AsyncSession,
    job_id: str,
    message: str,
    *,
    worker_id: str,
    submission_id: int | None = None,
) -> None:
    result = {"status": "SYSTEM_ERROR", "message": message[:4000]}
    updated = await db.execute(
        text(
            """
            UPDATE judge_jobs
            SET status = 'failed',
                result = CAST(:result AS json),
                last_error = :message,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = now()
            WHERE id = :job_id
              AND status = 'running'
              AND locked_by = :worker_id
            """
        ),
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "result": json.dumps(_json_safe(result)),
            "message": message.replace("\x00", "")[:4000],
        },
    )
    if not updated.rowcount:
        await db.rollback()
        return
    if submission_id is not None:
        await db.execute(
            text(
                """
                UPDATE submissions
                SET status = 'SYSTEM_ERROR', message = :message
                WHERE id = :submission_id
                  AND status = 'PENDING'
                """
            ),
            {"submission_id": submission_id, "message": message[:4000]},
        )
    await db.commit()


async def requeue_expired_jobs(db: AsyncSession) -> list[dict]:
    """Return expired jobs to the queue; fail them after max attempts."""
    retried = await db.execute(
        text(
            """
            UPDATE judge_jobs
            SET status = 'queued',
                locked_by = NULL,
                locked_until = NULL,
                available_at = now(),
                last_error = 'worker lease expired',
                updated_at = now()
            WHERE status = 'running'
              AND locked_until < now()
              AND attempts < :max_attempts
            RETURNING id
            """
        ),
        {"max_attempts": JOB_MAX_ATTEMPTS},
    )
    retried_ids = [row[0] for row in retried.all()]

    failed = await db.execute(
        text(
            """
            UPDATE judge_jobs
            SET status = 'failed',
                result = CAST(:result AS json),
                last_error = 'worker lease expired after max attempts',
                locked_by = NULL,
                locked_until = NULL,
                updated_at = now()
            WHERE status = 'running'
              AND locked_until < now()
              AND attempts >= :max_attempts
            RETURNING id, kind, submission_id
            """
        ),
        {
            "max_attempts": JOB_MAX_ATTEMPTS,
            "result": json.dumps(
                {
                    "status": "SYSTEM_ERROR",
                    "message": "worker lease expired after max attempts",
                }
            ),
        },
    )
    failed_rows = [dict(row) for row in failed.mappings().all()]

    for row in failed_rows:
        if row.get("submission_id") is not None:
            await db.execute(
                text(
                    """
                    UPDATE submissions
                    SET status = 'SYSTEM_ERROR',
                        message = 'worker lease expired after max attempts'
                    WHERE id = :submission_id
                      AND status = 'PENDING'
                    """
                ),
                {"submission_id": row["submission_id"]},
            )

    await db.commit()
    return [
        {"id": job_id, "requeued": True, "failed": False} for job_id in retried_ids
    ] + [
        {"id": row["id"], "requeued": False, "failed": True} for row in failed_rows
    ]


async def get_job(db: AsyncSession, job_id: str):
    result = await db.execute(
        text("SELECT * FROM judge_jobs WHERE id = :job_id"), {"job_id": job_id}
    )
    return result.mappings().first()

