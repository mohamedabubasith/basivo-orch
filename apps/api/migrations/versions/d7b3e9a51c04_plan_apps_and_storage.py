"""apps and storage are editable, and a plan can be added

Revision ID: d7b3e9a51c04
Revises: a2f5c31d8b47
Create Date: 2026-09-12 08:05:00.000000

The two limits that arrived with the App Builder were the only ones a platform
admin could not change without a deploy. The same table also now holds plans
that never shipped at all: a row whose code is not one of ours is a new tier
rather than an edit to an existing one, which is what makes adding one a form
rather than a release.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7b3e9a51c04"
down_revision: str | None = "a2f5c31d8b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plan_override", sa.Column("apps", sa.Integer(), nullable=True))
    op.add_column("plan_override", sa.Column("storage_mb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_override", "storage_mb")
    op.drop_column("plan_override", "apps")
