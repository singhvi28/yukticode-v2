import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from server.db.database import Base


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    problems = relationship("Problem", back_populates="author")
    submissions = relationship("Submission", back_populates="user")
    contests_created = relationship("Contest", back_populates="creator")


class Problem(Base):
    __tablename__ = "problems"

    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False, unique=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_published = Column(Boolean, default=False, nullable=False)

    difficulty = Column(String(20), default="Medium", nullable=False)
    tags = Column(Text, default="[]", nullable=False)
    statement = Column(Text, nullable=False, default="")
    time_limit_ms = Column(Integer, default=2000, nullable=False)
    memory_limit_mb = Column(Integer, default=256, nullable=False)

    author = relationship("User", back_populates="problems")
    submissions = relationship("Submission", back_populates="problem")
    test_cases = relationship(
        "TestCase", back_populates="problem", cascade="all, delete-orphan"
    )


class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(Integer, primary_key=True)
    problem_id = Column(
        Integer, ForeignKey("problems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_data = Column(Text, nullable=False)
    expected_output = Column(Text, nullable=False)
    is_sample = Column(Boolean, default=False, nullable=False)
    score = Column(Integer, default=10, nullable=False)

    problem = relationship("Problem", back_populates="test_cases")


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        Index(
            "ix_submissions_contest_user_problem_time",
            "contest_id",
            "user_id",
            "problem_id",
            "submitted_at",
        ),
        Index("ix_submissions_user_time", "user_id", "submitted_at"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_id = Column(
        Integer, ForeignKey("problems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language = Column(String(20), nullable=False)
    code = Column(Text, nullable=False)

    status = Column(String(20), default="PENDING", nullable=False, index=True)
    execution_time_ms = Column(Float, nullable=True)
    peak_memory_mb = Column(Float, nullable=True)
    message = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    contest_id = Column(
        Integer, ForeignKey("contests.id", ondelete="SET NULL"), nullable=True
    )

    user = relationship("User", back_populates="submissions")
    problem = relationship("Problem", back_populates="submissions")


class Contest(Base):
    __tablename__ = "contests"

    id = Column(Integer, primary_key=True)
    title = Column(String(150), nullable=False, unique=True)
    description = Column(Text, default="", nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=True)
    end_time = Column(DateTime(timezone=True), nullable=True)
    is_published = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)

    creator = relationship("User", back_populates="contests_created")
    contest_problems = relationship(
        "ContestProblem", back_populates="contest", cascade="all, delete-orphan"
    )
    registrations = relationship(
        "ContestRegistration", back_populates="contest", cascade="all, delete-orphan"
    )


class ContestRegistration(Base):
    __tablename__ = "contest_registrations"
    __table_args__ = (
        UniqueConstraint("contest_id", "user_id", name="uq_contest_registration"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contest_id = Column(
        Integer, ForeignKey("contests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    registered_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", backref="contest_registrations")
    contest = relationship("Contest", back_populates="registrations")


class ContestProblem(Base):
    __tablename__ = "contest_problems"
    __table_args__ = (
        UniqueConstraint("contest_id", "problem_id", name="uq_contest_problem"),
    )

    id = Column(Integer, primary_key=True)
    contest_id = Column(
        Integer, ForeignKey("contests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_id = Column(
        Integer, ForeignKey("problems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score = Column(Integer, default=100, nullable=False)
    display_order = Column(Integer, default=0, nullable=False)

    contest = relationship("Contest", back_populates="contest_problems")
    problem = relationship("Problem")


class JudgeJob(Base):
    """The queue. One row per unit of work; Postgres is the broker."""

    __tablename__ = "judge_jobs"
    __table_args__ = (
        Index("ix_judge_jobs_claim", "status", "available_at", "priority", "created_at"),
        Index("ix_judge_jobs_user", "user_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    kind = Column(String(20), nullable=False)  # submit, run, run_batch
    status = Column(String(20), default="queued", nullable=False, index=True)
    priority = Column(Integer, default=100, nullable=False)

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    submission_id = Column(
        Integer,
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    payload = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=True)

    attempts = Column(Integer, default=0, nullable=False)
    locked_by = Column(String(100), nullable=True)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    available_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
