import datetime
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from server.auth import get_current_user
from server.db.database import get_db_session
from server.db.models import (
    Contest,
    ContestProblem,
    JudgeJob,
    Problem,
    Submission,
    TestCase,
    User,
)
from server.jobs import get_job

router = APIRouter(prefix="/admin", tags=["admin"])


async def admin_required(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )
    return current_user


class ProblemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    statement: str = ""
    time_limit_ms: int = Field(default=2000, ge=100, le=10_000)
    memory_limit_mb: int = Field(default=256, ge=32, le=1024)
    difficulty: str = "Medium"
    tags: list[str] = Field(default_factory=list)


class ProblemUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    statement: Optional[str] = None
    time_limit_ms: Optional[int] = Field(default=None, ge=100, le=10_000)
    memory_limit_mb: Optional[int] = Field(default=None, ge=32, le=1024)
    difficulty: Optional[str] = None
    tags: Optional[list[str]] = None
    is_published: Optional[bool] = None


class TestCaseCreate(BaseModel):
    input_data: str = Field(..., max_length=1_000_000)
    expected_output: str = Field(..., max_length=1_000_000)
    is_sample: bool = False
    score: int = Field(default=10, ge=0)


class TestCaseUpdate(BaseModel):
    input_data: Optional[str] = Field(default=None, max_length=1_000_000)
    expected_output: Optional[str] = Field(default=None, max_length=1_000_000)
    is_sample: Optional[bool] = None
    score: Optional[int] = Field(default=None, ge=0)


class TestCaseRunRequest(BaseModel):
    language: str = Field(default="py", pattern="^(py|cpp|java)$")
    src_code: str = Field(..., max_length=262_144)


class ContestCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    description: str = ""
    start_time: Optional[datetime.datetime] = None
    end_time: Optional[datetime.datetime] = None


class ContestUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=150)
    description: Optional[str] = None
    start_time: Optional[datetime.datetime] = None
    end_time: Optional[datetime.datetime] = None
    is_published: Optional[bool] = None


class ContestProblemAdd(BaseModel):
    problem_id: int = Field(..., gt=0)
    score: int = Field(default=100, ge=0)
    display_order: int = 0


async def _get_problem(problem_id: int, db: AsyncSession) -> Problem:
    problem = await db.get(Problem, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    return problem


@router.get("/problems")
async def admin_list_problems(
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    problems = (
        await db.execute(select(Problem).order_by(Problem.id))
    ).scalars().all()
    return [
        {
            "id": p.id,
            "title": p.title,
            "is_published": p.is_published,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in problems
    ]


@router.post("/problems", status_code=201)
async def admin_create_problem(
    payload: ProblemCreate,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    existing = (
        await db.execute(select(Problem).where(Problem.title == payload.title))
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Problem title already exists")

    problem = Problem(
        title=payload.title,
        author_id=admin.id,
        is_published=False,
        statement=payload.statement or "",
        time_limit_ms=payload.time_limit_ms,
        memory_limit_mb=payload.memory_limit_mb,
        difficulty=payload.difficulty,
        tags=json.dumps(payload.tags),
    )
    db.add(problem)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Problem title already exists")
    await db.refresh(problem)
    return {"id": problem.id, "title": problem.title, "is_published": problem.is_published}


@router.get("/problems/{problem_id}")
async def admin_get_problem(
    problem_id: int,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    problem = await _get_problem(problem_id, db)
    try:
        tags = json.loads(problem.tags) if problem.tags else []
    except (TypeError, ValueError):
        tags = []
    return {
        "id": problem.id,
        "title": problem.title,
        "statement": problem.statement or "",
        "time_limit_ms": problem.time_limit_ms,
        "memory_limit_mb": problem.memory_limit_mb,
        "difficulty": problem.difficulty,
        "tags": tags,
        "is_published": problem.is_published,
    }


@router.patch("/problems/{problem_id}")
async def admin_update_problem(
    problem_id: int,
    payload: ProblemUpdate,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    problem = await _get_problem(problem_id, db)
    data = payload.model_dump(exclude_none=True)
    if data.get("is_published") is True:
        test_count = (
            await db.execute(
                select(func.count(TestCase.id)).where(
                    TestCase.problem_id == problem_id
                )
            )
        ).scalar_one()
        if test_count == 0:
            raise HTTPException(
                status_code=409,
                detail="Add at least one test case before publishing this problem",
            )
    if "tags" in data:
        data["tags"] = json.dumps(data["tags"])
    for key, value in data.items():
        setattr(problem, key, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Problem title already exists")
    return {"msg": "updated", "id": problem.id}


@router.delete("/problems/{problem_id}", status_code=204)
async def admin_delete_problem(
    problem_id: int,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    problem = await _get_problem(problem_id, db)
    await db.execute(Submission.__table__.delete().where(Submission.problem_id == problem_id))
    await db.execute(
        ContestProblem.__table__.delete().where(ContestProblem.problem_id == problem_id)
    )
    await db.delete(problem)
    await db.commit()


@router.get("/problems/{problem_id}/testcases")
async def admin_list_testcases(
    problem_id: int,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    await _get_problem(problem_id, db)
    test_cases = (
        await db.execute(
            select(TestCase)
            .where(TestCase.problem_id == problem_id)
            .order_by(TestCase.id)
        )
    ).scalars().all()
    return [
        {
            "id": tc.id,
            "input_data": tc.input_data,
            "expected_output": tc.expected_output,
            "is_sample": tc.is_sample,
            "score": tc.score,
        }
        for tc in test_cases
    ]


@router.post("/problems/{problem_id}/testcases", status_code=201)
async def admin_create_testcase(
    problem_id: int,
    payload: TestCaseCreate,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    await _get_problem(problem_id, db)
    test_case = TestCase(problem_id=problem_id, **payload.model_dump())
    db.add(test_case)
    await db.commit()
    await db.refresh(test_case)
    return {"id": test_case.id, "msg": "created"}


@router.put("/problems/{problem_id}/testcases/{tc_id}")
async def admin_update_testcase(
    problem_id: int,
    tc_id: int,
    payload: TestCaseUpdate,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    await _get_problem(problem_id, db)
    test_case = (
        await db.execute(
            select(TestCase).where(
                TestCase.id == tc_id, TestCase.problem_id == problem_id
            )
        )
    ).scalars().first()
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")

    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(test_case, key, value)
    await db.commit()
    return {"msg": "updated", "id": test_case.id}


@router.delete("/problems/{problem_id}/testcases/{tc_id}", status_code=204)
async def admin_delete_testcase(
    problem_id: int,
    tc_id: int,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    await _get_problem(problem_id, db)
    test_case = (
        await db.execute(
            select(TestCase).where(
                TestCase.id == tc_id, TestCase.problem_id == problem_id
            )
        )
    ).scalars().first()
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")
    await db.delete(test_case)
    await db.commit()


@router.post("/problems/{problem_id}/testcases/{tc_id}/run", status_code=202)
async def admin_run_testcase(
    problem_id: int,
    tc_id: int,
    payload: TestCaseRunRequest,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    problem = await _get_problem(problem_id, db)
    test_case = (
        await db.execute(
            select(TestCase).where(
                TestCase.id == tc_id, TestCase.problem_id == problem_id
            )
        )
    ).scalars().first()
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")

    job = JudgeJob(
        id=uuid.uuid4().hex,
        kind="run",
        user_id=admin.id,
        payload={
            "language": payload.language,
            "time_limit": problem.time_limit_ms,
            "memory_limit": problem.memory_limit_mb,
            "src_code": payload.src_code,
            "std_in": test_case.input_data,
            "expected_output": test_case.expected_output,
        },
        priority=20,
    )
    db.add(job)
    await db.commit()
    return {"run_id": job.id, "msg": "dry run queued"}


@router.get("/run-result/{run_id}")
async def admin_poll_run_result(
    run_id: str,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    job = await get_job(db, run_id)
    if not job or job["user_id"] != admin.id:
        raise HTTPException(status_code=404, detail="Run not found")
    if job["status"] in ("queued", "running"):
        return {"status": "pending"}
    if job["status"] == "failed":
        return {
            "status": "SYSTEM_ERROR",
            "verdict": "SYSTEM_ERROR",
            "message": job.get("last_error") or "run failed",
            "std_out": "",
        }

    result = job.get("result") or {}
    status_value = result.get("status", "SYSTEM_ERROR")
    return {
        **result,
        "status": status_value,
        "verdict": status_value,
        "worker_status": status_value,
        "std_out": result.get("std_out", ""),
        "expected": result.get("expected", ""),
    }


@router.get("/contests")
async def admin_list_contests(
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    contests = (
        await db.execute(select(Contest).order_by(Contest.id))
    ).scalars().all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "start_time": c.start_time.isoformat() if c.start_time else None,
            "end_time": c.end_time.isoformat() if c.end_time else None,
            "is_published": c.is_published,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in contests
    ]


@router.post("/contests", status_code=201)
async def admin_create_contest(
    payload: ContestCreate,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    if payload.start_time and payload.end_time and payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    existing = (
        await db.execute(select(Contest).where(Contest.title == payload.title))
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Contest title already exists")

    contest = Contest(
        title=payload.title,
        description=payload.description,
        start_time=payload.start_time,
        end_time=payload.end_time,
        created_by=admin.id,
    )
    db.add(contest)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Contest title already exists")
    await db.refresh(contest)
    return {"id": contest.id, "title": contest.title}


@router.get("/contests/{contest_id}")
async def admin_get_contest(
    contest_id: int,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    contest = await db.get(Contest, contest_id)
    if not contest:
        raise HTTPException(status_code=404, detail="Contest not found")

    links = (
        await db.execute(
            select(ContestProblem, Problem)
            .join(Problem, Problem.id == ContestProblem.problem_id)
            .where(ContestProblem.contest_id == contest_id)
            .order_by(ContestProblem.display_order, ContestProblem.id)
        )
    ).all()
    return {
        "id": contest.id,
        "title": contest.title,
        "description": contest.description,
        "start_time": contest.start_time.isoformat() if contest.start_time else None,
        "end_time": contest.end_time.isoformat() if contest.end_time else None,
        "is_published": contest.is_published,
        "problems": [
            {
                "id": problem.id,
                "title": problem.title,
                "score": link.score,
                "display_order": link.display_order,
                "cp_id": link.id,
            }
            for link, problem in links
        ],
    }


@router.patch("/contests/{contest_id}")
async def admin_update_contest(
    contest_id: int,
    payload: ContestUpdate,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    contest = await db.get(Contest, contest_id)
    if not contest:
        raise HTTPException(status_code=404, detail="Contest not found")

    data = payload.model_dump(exclude_unset=True)
    start = data.get("start_time", contest.start_time)
    end = data.get("end_time", contest.end_time)
    if start and end and end <= start:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    for key, value in data.items():
        setattr(contest, key, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Contest title already exists")
    return {"msg": "updated", "id": contest.id}


@router.delete("/contests/{contest_id}", status_code=204)
async def admin_delete_contest(
    contest_id: int,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    contest = await db.get(Contest, contest_id)
    if not contest:
        raise HTTPException(status_code=404, detail="Contest not found")
    await db.delete(contest)
    await db.commit()


@router.post("/contests/{contest_id}/problems", status_code=201)
async def admin_add_contest_problem(
    contest_id: int,
    payload: ContestProblemAdd,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    if not await db.get(Contest, contest_id):
        raise HTTPException(status_code=404, detail="Contest not found")
    if not await db.get(Problem, payload.problem_id):
        raise HTTPException(status_code=404, detail="Problem not found")

    db.add(
        ContestProblem(
            contest_id=contest_id,
            problem_id=payload.problem_id,
            score=payload.score,
            display_order=payload.display_order,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Problem already in contest")
    return {"msg": "problem added to contest"}


@router.delete("/contests/{contest_id}/problems/{problem_id}", status_code=204)
async def admin_remove_contest_problem(
    contest_id: int,
    problem_id: int,
    admin: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db_session),
):
    link = (
        await db.execute(
            select(ContestProblem).where(
                ContestProblem.contest_id == contest_id,
                ContestProblem.problem_id == problem_id,
            )
        )
    ).scalars().first()
    if not link:
        raise HTTPException(status_code=404, detail="Association not found")
    await db.delete(link)
    await db.commit()
