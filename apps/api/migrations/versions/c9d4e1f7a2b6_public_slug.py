"""a public address for every app

Revision ID: c9d4e1f7a2b6
Revises: b7f0c2a4e913
Create Date: 2026-09-12 01:20:00.000000

The published address used to be the private preview link. Now it is a slug a
person can read aloud, unique across every workspace. Existing projects get
theirs from their name and four characters of their id, which is unique for
the same reason the id is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d4e1f7a2b6"
down_revision: str | None = "b7f0c2a4e913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_project", sa.Column("public_slug", sa.String(length=80), nullable=True))
    op.execute(
        "UPDATE app_project SET public_slug = "
        "left(slug, 60) || '-' || substr(replace(id::text, '-', ''), 1, 4)"
    )
    op.alter_column("app_project", "public_slug", nullable=False)
    op.create_index(op.f("ix_app_project_public_slug"), "app_project", ["public_slug"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_app_project_public_slug"), table_name="app_project")
    op.drop_column("app_project", "public_slug")
