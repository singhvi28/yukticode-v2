import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload

from server.auth import get_current_user
from server.db.database import get_db_session
from server.db.models import (
    Contest,
    ContestProblem,
    ContestRegistration,
    JudgeJob,
    Problem,
    Submission,
    TestCase,
    User,
)
from server.jobs import get_job
from server.models import RunBatchRequest, RunRequest, SubmitRequest

router = APIRouter()

MAX_ACTIVE_JOBS = 5


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _published_problem(db: AsyncSession, problem_id: int) -> Problem:
    problem = (
        await db.execute(
            select(Problem).where(
                Problem.id == problem_id, Problem.is_published.is_(True)
            )
        )
    ).scalars().first()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    return problem


async def _contest_gate(
    db: AsyncSession, problem_id: int, contest_id: int, user_id: int
) -> None:
    contest = (
        await db.execute(
            select(Contest).where(
                Contest.id == contest_id, Contest.is_published.is_(True)
            )
        )
    ).scalars().first()
    if not contest:
        raise HTTPException(status_code=404, detail="Contest not found")

    link = (
        await db.execute(
            select(ContestProblem).where(
                ContestProblem.contest_id == contest_id,
                ContestProblem.problem_id == problem_id,
            )
        )
    ).scalars().first()
    if not link:
        raise HTTPException(status_code=404, detail="Problem is not in this contest")

    registration = (
        await db.execute(
            select(ContestRegistration).where(
                ContestRegistration.contest_id == contest_id,
                ContestRegistration.user_id == user_id,
            )
        )
    ).scalars().first()
    if not registration:
        raise HTTPException(
            status_code=403, detail="Register for the contest before submitting"
        )

    now = datetime.now(timezone.utc)
    if contest.start_time and now < _as_aware(contest.start_time):
        raise HTTPException(
            status_code=403, detail="This contest has not started yet"
        )
    if contest.end_time and now >= _as_aware(contest.end_time):
        raise HTTPException(status_code=403, detail="This contest has ended")


async def _check_active_jobs(db: AsyncSession, user: User) -> None:
    if user.is_admin:
        return
    active = (
        await db.execute(
            select(func.count(JudgeJob.id)).where(
                JudgeJob.user_id == user.id,
                JudgeJob.status.in_(("queued", "running")),
            )
        )
    ).scalar_one()
    if active >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail="Too many queued jobs; wait for one to finish.",
        )


def _new_job(
    *,
    kind: str,
    user_id: int,
    payload: dict,
    submission_id: int | None = None,
    priority: int = 100,
) -> JudgeJob:
    return JudgeJob(
        id=str(uuid.uuid4()),
        kind=kind,
        user_id=user_id,
        submission_id=submission_id,
        payload=payload,
        priority=priority,
    )


@router.post("/submit", status_code=202)
async def submit(
    body: SubmitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    problem = await _published_problem(db, body.problem_id)
    if body.contest_id is not None:
        await _contest_gate(db, body.problem_id, body.contest_id, current_user.id)
    await _check_active_jobs(db, current_user)

    test_count = (
        await db.execute(
            select(func.count(TestCase.id)).where(TestCase.problem_id == problem.id)
        )
    ).scalar_one()
    if test_count == 0:
        raise HTTPException(
            status_code=409,
            detail="This problem has no test cases; ask an admin to add some.",
        )

    submission = Submission(
        user_id=current_user.id,
        problem_id=problem.id,
        language=body.language,
        code=body.src_code,
        status="PENDING",
        contest_id=body.contest_id,
    )
    db.add(submission)
    await db.flush()

    job = _new_job(
        kind="submit",
        user_id=current_user.id,
        submission_id=submission.id,
        payload={
            "submission_id": submission.id,
            "language": body.language,
            "time_limit": problem.time_limit_ms,
            "memory_limit": problem.memory_limit_mb,
        },
        priority=10,
    )
    db.add(job)
    await db.commit()
    return {"msg": "submission queued", "submission_id": submission.id, "job_id": job.id}


@router.post("/run", status_code=202)
async def run(
    body: RunRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    await _check_active_jobs(db, current_user)
    job = _new_job(
        kind="run",
        user_id=current_user.id,
        payload={
            "language": body.language,
            "time_limit": body.time_limit,
            "memory_limit": body.memory_limit,
            "src_code": body.src_code,
            "std_in": body.std_in,
        },
    )
    db.add(job)
    await db.commit()
    return {"msg": "run queued", "run_id": job.id}


@router.post("/run_batch", status_code=202)
async def run_batch(
    body: RunBatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    await _check_active_jobs(db, current_user)
    job = _new_job(
        kind="run_batch",
        user_id=current_user.id,
        payload={
            "language": body.language,
            "time_limit": body.time_limit,
            "memory_limit": body.memory_limit,
            "src_code": body.src_code,
            "tests": [test.model_dump() for test in body.tests],
        },
    )
    db.add(job)
    await db.commit()
    return {"msg": "batch queued", "batch_id": job.id, "run_id": job.id}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    job = await get_job(db, run_id)
    if not job or (job["user_id"] != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Run not found")

    if job["status"] in ("queued", "running"):
        return {"status": "PENDING", "run_id": run_id}
    if job["status"] == "failed":
        return {
            "status": "SYSTEM_ERROR",
            "run_id": run_id,
            "message": job.get("last_error") or "run failed",
        }

    result = job.get("result") or {}
    if job["kind"] == "run_batch":
        return {"status": "DONE", "run_id": run_id, "tests": result.get("tests", [])}
    return {**result, "run_id": run_id}


@router.get("/problems")
async def list_problems(db: AsyncSession = Depends(get_db_session)):
    stmt = (
        select(
            Problem,
            func.count(Submission.id).filter(Submission.status == "AC").label("ac_count"),
            func.count(Submission.id).label("total_count"),
        )
        .outerjoin(Submission, Submission.problem_id == Problem.id)
        .where(Problem.is_published.is_(True))
        .group_by(Problem.id)
        .order_by(Problem.id)
    )
    rows = (await db.execute(stmt)).all()

    response = []
    for problem, ac_count, total_count in rows:
        acceptance = (ac_count / total_count * 100) if total_count else 0.0
        try:
            tags = json.loads(problem.tags) if problem.tags else []
        except (TypeError, ValueError):
            tags = []
        response.append(
            {
                "id": problem.id,
                "title": problem.title,
                "difficulty": problem.difficulty or "Medium",
                "acceptance": round(acceptance, 1),
                "tags": tags,
            }
        )
    return response


@router.get("/problems/{problem_id}")
async def get_problem(
    problem_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    problem = await _published_problem(db, problem_id)

    now = datetime.now(timezone.utc)
    published_contests = (
        await db.execute(
            select(Contest)
            .join(ContestProblem, Contest.id == ContestProblem.contest_id)
            .where(
                ContestProblem.problem_id == problem_id,
                Contest.is_published.is_(True),
            )
        )
    ).scalars().all()
    for contest in published_contests:
        if contest.start_time and now < _as_aware(contest.start_time):
            raise HTTPException(
                status_code=403, detail="This problem is locked until the contest starts."
            )

    sample_rows = (
        await db.execute(
            select(TestCase)
            .where(TestCase.problem_id == problem.id, TestCase.is_sample.is_(True))
            .order_by(TestCase.id)
        )
    ).scalars().all()

    return {
        "id": problem.id,
        "title": problem.title,
        "timeLimit": problem.time_limit_ms,
        "memoryLimit": problem.memory_limit_mb,
        "statement": problem.statement or "No statement available.",
        "samples": [
            {"id": tc.id, "input": tc.input_data, "output": tc.expected_output}
            for tc in sample_rows
        ],
    }


@router.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    conditions = [Submission.id == submission_id]
    if not current_user.is_admin:
        conditions.append(Submission.user_id == current_user.id)
    submission = (
        await db.execute(select(Submission).where(*conditions))
    ).scalars().first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    return {
        "id": submission.id,
        "status": submission.status,
        "execution_time_ms": submission.execution_time_ms,
        "peak_memory_mb": submission.peak_memory_mb,
        "message": submission.message or "",
    }


@router.get("/submissions")
async def list_submissions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    submissions = (
        await db.execute(
            select(Submission)
            .options(joinedload(Submission.problem))
            .where(Submission.user_id == current_user.id)
            .order_by(Submission.submitted_at.desc())
        )
    ).unique().scalars().all()

    return [
        {
            "id": sub.id,
            "problem_id": sub.problem_id,
            "problem_title": sub.problem.title if sub.problem else "Unknown",
            "status": sub.status,
            "language": sub.language,
            "time": f"{sub.execution_time_ms:.1f}ms" if sub.execution_time_ms else "-",
            "memory": f"{sub.peak_memory_mb:.1f}MB" if sub.peak_memory_mb else "-",
            "date": sub.submitted_at.isoformat() if sub.submitted_at else "",
        }
        for sub in submissions
    ]
