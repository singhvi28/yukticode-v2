from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from server.leaderboard import rank_submissions


def _contest():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(start_time=start)


def _submission(user_id, problem_id, status, minutes, row_id=1):
    return SimpleNamespace(
        id=row_id,
        user_id=user_id,
        problem_id=problem_id,
        status=status,
        submitted_at=_contest().start_time + timedelta(minutes=minutes),
    )


def test_only_solved_problems_score_and_penalty_uses_first_ac():
    rows = [
        _submission(1, 10, "WA", 1, 1),
        _submission(1, 10, "AC", 10, 2),
        _submission(1, 10, "WA", 12, 3),  # ignored after AC
        _submission(2, 10, "AC", 5, 4),
        _submission(3, 10, "WA", 2, 5),
    ]
    table = rank_submissions(rows, _contest(), {10: 100}, {1: "alice", 2: "bob", 3: "cara"})

    assert [row["username"] for row in table] == ["bob", "alice"]
    assert table[0]["score"] == 100
    assert table[0]["penalty"] == 5
    assert table[1]["score"] == 100
    assert table[1]["penalty"] == 30  # 10 minutes + 20 penalty


def test_multiple_problems_sum_score_and_penalty():
    rows = [
        _submission(1, 10, "AC", 1, 1),
        _submission(1, 20, "AC", 60, 2),
        _submission(2, 20, "AC", 2, 3),
    ]
    table = rank_submissions(
        rows, _contest(), {10: 50, 20: 100}, {1: "alice", 2: "bob"}
    )

    by_name = {row["username"]: row for row in table}
    assert by_name["alice"]["score"] == 150
    assert by_name["alice"]["penalty"] == 61
    assert by_name["bob"]["score"] == 100
    assert table[0]["username"] == "alice"


def test_rank_is_deterministic_for_exact_ties():
    rows = [
        _submission(2, 10, "AC", 3, 1),
        _submission(1, 10, "AC", 3, 2),
    ]
    table = rank_submissions(
        rows, _contest(), {10: 100}, {1: "alice", 2: "bob"}
    )
    assert [row["username"] for row in table] == ["alice", "bob"]
    assert [row["rank"] for row in table] == [1, 2]


def test_unsolved_and_unknown_problem_are_not_ranked():
    rows = [
        _submission(1, 10, "WA", 1, 1),
        _submission(2, 99, "AC", 1, 2),  # no score for problem 99
    ]
    table = rank_submissions(rows, _contest(), {10: 100}, {1: "alice", 2: "bob"})
    assert table == []
