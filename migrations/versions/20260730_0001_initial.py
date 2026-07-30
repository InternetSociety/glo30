"""Create users and cached tiles.

Revision ID: 20260730_0001
Revises:
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260730_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("bearer_token", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_bearer_token"), "users", ["bearer_token"], unique=True)

    op.create_table(
        "cached_tiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tile_id", sa.String(length=40), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_cached_tiles_tile_id"), "cached_tiles", ["tile_id"], unique=True
    )
    op.create_index(
        op.f("ix_cached_tiles_expires_at"), "cached_tiles", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_cached_tiles_expires_at"), table_name="cached_tiles")
    op.drop_index(op.f("ix_cached_tiles_tile_id"), table_name="cached_tiles")
    op.drop_table("cached_tiles")
    op.drop_index(op.f("ix_users_bearer_token"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
