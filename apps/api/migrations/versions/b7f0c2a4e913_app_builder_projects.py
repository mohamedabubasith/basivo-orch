"""app builder projects, versions and turns

Revision ID: b7f0c2a4e913
Revises: aed92f3d5c7c
Create Date: 2026-09-12 00:34:00.000000

Three tables and one column. The column is `flow.system`: an App Builder
project owns a flow with a single node, and that flow must not appear in the
flows list, where the only thing a person could do with it is break their app.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f0c2a4e913"
down_revision: str | None = "aed92f3d5c7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "flow",
        sa.Column("system", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.create_table(
        "app_project",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("flow_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source_artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("published_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["flow_id"], ["flow.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifact.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "slug", name="uq_app_project_org_slug"),
    )
    op.create_index(
        op.f("ix_app_project_organization_id"), "app_project", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_app_project_org_updated", "app_project", ["organization_id", "updated_at"], unique=False
    )

    op.create_table(
        "app_version",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("build_artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["build_artifact_id"], ["artifact.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["app_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifact.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version", name="uq_app_version"),
    )
    op.create_index(op.f("ix_app_version_project_id"), "app_version", ["project_id"], unique=False)

    # Added after `app_version` exists, which is why the model declares it with
    # `use_alter`: the two tables point at each other.
    op.create_foreign_key(
        "fk_app_project_published_version",
        "app_project",
        "app_version",
        ["published_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "app_turn",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "built", "failed", name="turnstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["app_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["version_id"], ["app_version.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_app_turn_project_id"), "app_turn", ["project_id"], unique=False)
    op.create_index(
        "ix_app_turn_project_created", "app_turn", ["project_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_app_turn_project_created", table_name="app_turn")
    op.drop_index(op.f("ix_app_turn_project_id"), table_name="app_turn")
    op.drop_table("app_turn")
    op.drop_constraint("fk_app_project_published_version", "app_project", type_="foreignkey")
    op.drop_index(op.f("ix_app_version_project_id"), table_name="app_version")
    op.drop_table("app_version")
    op.drop_index("ix_app_project_org_updated", table_name="app_project")
    op.drop_index(op.f("ix_app_project_organization_id"), table_name="app_project")
    op.drop_table("app_project")
    op.drop_column("flow", "system")
