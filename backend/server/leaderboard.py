"""Contest standings computed from submissions, not from a cache."""

from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import (
    Contest,
    ContestProblem,
    ContestRegistration,
    Submission,
    User,
)

PENALTY_STATUSES = {"WA", "TLE", "RE", "MLE"}
PENALTY_MINUTES = 20


def rank_submissions(rows, contest, scores: dict[int, int], names: dict[int, str]):
    """Pure scoring function; easy to test without a database."""
    per_problem: dict[tuple[int, int], dict] = {}
    for row in rows:
        key = (row.user_id, row.problem_id)
        state = per_problem.setdefault(
            key, {"solved": False, "attempts": 0, "first_ac": None}
        )
        if state["solved"]:
            continue
        if row.status == "AC":
            state["solved"] = True
            state["first_ac"] = row.submitted_at
        elif row.status in PENALTY_STATUSES:
            state["attempts"] += 1

    users: dict[int, dict] = {}
    for (user_id, problem_id), state in per_problem.items():
        if not state["solved"] or problem_id not in scores:
            continue
        user = users.setdefault(
            user_id,
            {"score": 0, "penalty": 0, "last_ac": contest.start_time},
        )
        user["score"] += scores[problem_id]
        minutes = int(
            (state["first_ac"] - contest.start_time).total_seconds() // 60
        )
        user["penalty"] += minutes + PENALTY_MINUTES * state["attempts"]
        user["last_ac"] = max(user["last_ac"], state["first_ac"])

    table = [
        {
            "user_id": user_id,
            "username": names.get(user_id, "unknown"),
            "score": data["score"],
            "penalty": data["penalty"],
            "last_ac": data["last_ac"],
        }
        for user_id, data in users.items()
    ]
    table.sort(key=lambda row: (-row["score"], row["penalty"], row["last_ac"], row["username"]))
    for rank, row in enumerate(table, start=1):
        row["rank"] = rank
    return table


async def compute_leaderboard(db: AsyncSession, contest_id: int):
    contest = await db.get(Contest, contest_id)
    if not contest or not contest.is_published:
        raise HTTPException(status_code=404, detail="Contest not found")
    if not contest.start_time or not contest.end_time:
        return []

    rows = (
        await db.execute(
            select(Submission)
            .join(
                ContestRegistration,
                and_(
                    ContestRegistration.user_id == Submission.user_id,
                    ContestRegistration.contest_id == contest_id,
                ),
            )
            .where(
                Submission.contest_id == contest_id,
                Submission.submitted_at >= contest.start_time,
                Submission.submitted_at < contest.end_time,
                Submission.status != "PENDING",
            )
            .order_by(
                Submission.user_id,
                Submission.problem_id,
                Submission.submitted_at,
                Submission.id,
            )
        )
    ).scalars().all()

    scores = {
        cp.problem_id: cp.score
        for cp in (
            await db.execute(
                select(ContestProblem).where(ContestProblem.contest_id == contest_id)
            )
        ).scalars().all()
    }

    user_ids = {row.user_id for row in rows}
    names: dict[int, str] = {}
    if user_ids:
        names = dict(
            (
                await db.execute(
                    select(User.id, User.username).where(User.id.in_(user_ids))
                )
            ).all()
        )

    return rank_submissions(rows, contest, scores, names)
