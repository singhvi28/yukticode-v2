import asyncio
import json
import logging
import os
import socket
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.future import select

from server.config import DATABASE_URL, JOB_LEASE_SECONDS
from server.db.models import Problem, Submission, TestCase
from server.jobs import claim_job, complete_job, fail_job, requeue_expired_jobs
from worker import judger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for noisy in ("docker", "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("worker")

REAP_INTERVAL_SECONDS = 300


async def fetch_submission_data(db: AsyncSession, submission_id: int) -> dict | None:
    submission = await db.get(Submission, submission_id)
    if not submission:
        return None
    problem = await db.get(Problem, submission.problem_id)
    if not problem:
        return None
    test_cases = (
        await db.execute(
            select(TestCase)
            .where(TestCase.problem_id == problem.id)
            .order_by(TestCase.id)
        )
    ).scalars().all()
    return {
        "language": submission.language,
        "source": submission.code or "",
        "time_limit_ms": problem.time_limit_ms,
        "memory_limit_mb": problem.memory_limit_mb,
        "test_cases": [
            {"input": tc.input_data, "expected_output": tc.expected_output}
            for tc in test_cases
        ],
    }


def _payload(job: dict) -> dict:
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        return json.loads(payload)
    return payload


async def process_job(job: dict, worker_id: str) -> None:
    job_id = job["id"]
    kind = job["kind"]
    payload = _payload(job)
    submission_id = job.get("submission_id")
    logger.info("processing job %s kind=%s attempt=%s", job_id, kind, job.get("attempts"))

    engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as db:
            if kind == "submit":
                data = await fetch_submission_data(db, payload["submission_id"])
                if not data:
                    await fail_job(
                        db,
                        job_id,
                        "submission or problem disappeared before judging",
                        worker_id=worker_id,
                        submission_id=submission_id,
                    )
                    return
                result = await asyncio.to_thread(
                    judger.run_submit,
                    language=data["language"],
                    source=data["source"],
                    test_cases=data["test_cases"],
                    time_limit_ms=data["time_limit_ms"],
                    memory_limit_mb=data["memory_limit_mb"],
                )
                # The source owner does not need the full stdout stored on the
                # submission; message and metrics are enough for the UI.
                result.pop("std_out", None)
                await complete_job(
                    db,
                    job_id,
                    result,
                    worker_id=worker_id,
                    submission_id=payload["submission_id"],
                )
                return

            if kind == "run":
                result = await asyncio.to_thread(
                    judger.run_custom,
                    language=payload["language"],
                    source=payload["src_code"],
                    stdin=payload.get("std_in", " "),
                    expected=payload.get("expected_output"),
                    time_limit_ms=int(payload["time_limit"]),
                    memory_limit_mb=int(payload["memory_limit"]),
                )
                await complete_job(db, job_id, result, worker_id=worker_id)
                return

            if kind == "run_batch":
                result = await asyncio.to_thread(
                    judger.run_batch,
                    language=payload["language"],
                    source=payload["src_code"],
                    tests=payload.get("tests", []),
                    time_limit_ms=int(payload["time_limit"]),
                    memory_limit_mb=int(payload["memory_limit"]),
                )
                await complete_job(db, job_id, result, worker_id=worker_id)
                return

            await fail_job(
                db,
                job_id,
                f"unknown job kind: {kind}",
                worker_id=worker_id,
                submission_id=submission_id,
            )
    except Exception as exc:
        logger.exception("job %s failed", job_id)
        async with Session() as db:
            await fail_job(
                db, job_id, str(exc), worker_id=worker_id, submission_id=submission_id
            )
    finally:
        await engine.dispose()


async def run() -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    logger.info("worker %s starting", worker_id)

    try:
        judger.ensure_image(judger._get_client())
    except Exception:
        logger.exception("judge image is not available yet; will retry on first job")

    engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    last_reap = 0.0

    try:
        while True:
            try:
                async with Session() as db:
                    # Recover jobs whose worker died, even when newer work is
                    # waiting in the queue.
                    await requeue_expired_jobs(db)
                    job = await claim_job(db, worker_id, JOB_LEASE_SECONDS)

                if job:
                    await process_job(job, worker_id)
                    continue

                if time.monotonic() - last_reap > REAP_INTERVAL_SECONDS:
                    last_reap = time.monotonic()
                    removed = await asyncio.to_thread(judger.reap_orphaned_containers)
                    if removed:
                        logger.info("reaped %d orphaned judge container(s)", removed)

                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker loop error")
                await asyncio.sleep(2.0)
    finally:
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
