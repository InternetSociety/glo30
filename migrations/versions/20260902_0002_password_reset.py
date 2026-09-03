"""Add password-reset fields to users.

Revision ID: 20260902_0002
Revises: 20260730_0001
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0002"
down_revision: str | None = "20260730_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("reset_token_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "users",
        sa.Column("reset_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_users_reset_token_hash"),
        "users",
        ["reset_token_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_users_reset_token_hash"), table_name="users")
    op.drop_column("users", "reset_token_expires_at")
    op.drop_column("users", "reset_token_hash")
