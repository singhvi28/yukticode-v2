from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Language = Literal["py", "cpp", "java"]

MAX_SOURCE_BYTES = 262_144
MAX_TEST_BYTES = 65_536


class SubmitRequest(BaseModel):
    problem_id: int = Field(..., gt=0)
    language: Language
    src_code: str = Field(..., max_length=MAX_SOURCE_BYTES)
    contest_id: Optional[int] = Field(default=None, gt=0)


class RunRequest(BaseModel):
    language: Language
    time_limit: int = Field(..., ge=100, le=10_000)
    memory_limit: int = Field(..., ge=32, le=1024)
    src_code: str = Field(..., max_length=MAX_SOURCE_BYTES)
    std_in: str = Field(default=" ", max_length=MAX_TEST_BYTES)


class RunBatchTestCase(BaseModel):
    input: str = Field(default=" ", max_length=MAX_TEST_BYTES)
    expected_output: Optional[str] = Field(default=None, max_length=MAX_TEST_BYTES)


class RunBatchRequest(BaseModel):
    language: Language
    time_limit: int = Field(..., ge=100, le=10_000)
    memory_limit: int = Field(..., ge=32, le=1024)
    src_code: str = Field(..., max_length=MAX_SOURCE_BYTES)
    tests: list[RunBatchTestCase] = Field(..., min_length=1, max_length=20)


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def username_is_simple(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.replace("_", "").replace("-", "").isalnum():
            raise ValueError("username may contain only letters, numbers, _ and -")
        return cleaned


class UserLogin(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool
    is_admin: bool
