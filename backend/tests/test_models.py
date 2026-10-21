import pytest
from pydantic import ValidationError

from server.models import RunBatchRequest, RunRequest, SubmitRequest, UserCreate


def test_language_allowlist_rejects_unknown_language():
    with pytest.raises(ValidationError):
        SubmitRequest(problem_id=1, language="ruby", src_code="puts 1")


def test_source_size_limit_is_enforced():
    with pytest.raises(ValidationError):
        SubmitRequest(problem_id=1, language="py", src_code="x" * 262_145)


def test_run_limits_are_bounded():
    with pytest.raises(ValidationError):
        RunRequest(language="py", time_limit=50, memory_limit=256, src_code="x")
    with pytest.raises(ValidationError):
        RunRequest(language="py", time_limit=2000, memory_limit=5000, src_code="x")


def test_batch_requires_between_one_and_twenty_tests():
    with pytest.raises(ValidationError):
        RunBatchRequest(
            language="py", time_limit=2000, memory_limit=256, src_code="x", tests=[]
        )
    with pytest.raises(ValidationError):
        RunBatchRequest(
            language="py",
            time_limit=2000,
            memory_limit=256,
            src_code="x",
            tests=[{"input": " "} for _ in range(21)],
        )


def test_username_validation_and_password_minimum():
    with pytest.raises(ValidationError):
        UserCreate(username="bad name", email="a@b.c", password="longenough")
    with pytest.raises(ValidationError):
        UserCreate(username="valid", email="a@b.c", password="short")
    user = UserCreate(username="valid_name", email="a@b.c", password="longenough")
    assert user.username == "valid_name"
