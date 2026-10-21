"""Initial schema for cf-clone-v2.

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

from typing import Sequence, Union

from alembic import op

from server.db.database import Base
import server.db.models  # noqa: F401 - register all tables

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The initial schema is small enough to keep in one place. Later changes
    # should be explicit Alembic operations.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
