"""a run can be asked to stop

Revision ID: b8e4f2c17d93
Revises: d7b3e9a51c04
Create Date: 2026-09-12 05:10:00.000000

`cancelled` was in the status enum from the first migration and nothing ever
set it: there was no way to stop a run that had started. The flag is how the
request reaches the worker holding it, which may be on another machine.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e4f2c17d93"
down_revision: str | None = "d7b3e9a51c04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("run", "cancel_requested")
