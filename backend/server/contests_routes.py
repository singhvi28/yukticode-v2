from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from server.auth import get_current_user, get_current_user_optional
from server.db.database import get_db_session
from server.db.models import (
    Contest,
    ContestProblem,
    ContestRegistration,
    Problem,
    Submission,
    User,
)
from server.leaderboard import compute_leaderboard

router = APIRouter(prefix="/contests", tags=["contests"])


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("")
async def list_public_contests(
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db_session),
):
    contests = (
        await db.execute(
            select(Contest)
            .where(Contest.is_published.is_(True))
            .order_by(Contest.start_time.desc())
        )
    ).scalars().all()

    registered_ids: set[int] = set()
    if current_user:
        registered_ids = {
            row[0]
            for row in (
                await db.execute(
                    select(ContestRegistration.contest_id).where(
                        ContestRegistration.user_id == current_user.id
                    )
                )
            ).all()
        }

    return [
        {
            "id": contest.id,
            "title": contest.title,
            "description": contest.description or "",
            "start_time": contest.start_time.isoformat() if contest.start_time else None,
            "end_time": contest.end_time.isoformat() if contest.end_time else None,
            "is_registered": contest.id in registered_ids,
        }
        for contest in contests
    ]


@router.get("/{contest_id}")
async def get_public_contest(
    contest_id: int,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db_session),
):
    contest = await db.get(Contest, contest_id)
    if not contest or not contest.is_published:
        raise HTTPException(status_code=404, detail="Contest not found")

    stats: dict[int, dict] = {}
    is_registered = False
    if current_user:
        is_registered = (
            await db.execute(
                select(ContestRegistration.id).where(
                    ContestRegistration.contest_id == contest_id,
                    ContestRegistration.user_id == current_user.id,
                )
            )
        ).scalars().first() is not None
        rows = (
            await db.execute(
                select(Submission.problem_id, Submission.status).where(
                    Submission.user_id == current_user.id,
                    Submission.contest_id == contest_id,
                )
            )
        ).all()
        for problem_id, status in rows:
            bucket = stats.setdefault(problem_id, {"attempts": 0, "solved": False})
            bucket["attempts"] += 1
            if status == "AC":
                bucket["solved"] = True

    links = (
        await db.execute(
            select(ContestProblem, Problem)
            .join(Problem, Problem.id == ContestProblem.problem_id)
            .where(ContestProblem.contest_id == contest_id)
            .order_by(ContestProblem.display_order, ContestProblem.id)
        )
    ).all()

    problems = []
    for link, problem in links:
        bucket = stats.get(problem.id, {"attempts": 0, "solved": False})
        problems.append(
            {
                "id": problem.id,
                "title": problem.title,
                "score": link.score,
                "display_order": link.display_order,
                "attempts": bucket["attempts"],
                "solved": bucket["solved"],
            }
        )

    return {
        "id": contest.id,
        "title": contest.title,
        "description": contest.description or "",
        "start_time": contest.start_time.isoformat() if contest.start_time else None,
        "end_time": contest.end_time.isoformat() if contest.end_time else None,
        "is_registered": is_registered,
        "problems": problems,
    }


@router.post("/{contest_id}/register")
async def register_for_contest(
    contest_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    contest = await db.get(Contest, contest_id)
    if not contest or not contest.is_published:
        raise HTTPException(status_code=404, detail="Contest not found")

    existing = (
        await db.execute(
            select(ContestRegistration).where(
                ContestRegistration.contest_id == contest_id,
                ContestRegistration.user_id == current_user.id,
            )
        )
    ).scalars().first()
    if existing:
        raise HTTPException(
            status_code=400, detail="You are already registered for this contest"
        )

    if contest.end_time and datetime.now(timezone.utc) >= _aware(contest.end_time):
        raise HTTPException(status_code=400, detail="This contest has ended")

    db.add(
        ContestRegistration(
            user_id=current_user.id,
            contest_id=contest_id,
            registered_at=datetime.now(timezone.utc),
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=400, detail="You are already registered for this contest"
        )
    return {"msg": "Successfully registered for the contest", "contest_id": contest_id}


@router.get("/{contest_id}/leaderboard")
async def fetch_leaderboard(
    contest_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    return {"leaderboard": await compute_leaderboard(db, contest_id)}
