"""images uploaded for an app to use

Revision ID: a2f5c31d8b47
Revises: c9d4e1f7a2b6
Create Date: 2026-09-12 07:10:00.000000

A photograph is something a model cannot draw, so people upload one. Stored
here rather than inside the project's tree: the tree is copied into every
version, and a logo that lived there would be kept once per build.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2f5c31d8b47"
down_revision: str | None = "c9d4e1f7a2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_asset",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=120), nullable=False),
        sa.Column("content_type", sa.String(length=80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["app_project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "filename", name="uq_app_asset_name"),
    )
    op.create_index(op.f("ix_app_asset_project_id"), "app_asset", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_app_asset_project_id"), table_name="app_asset")
    op.drop_table("app_asset")
