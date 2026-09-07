"""billing subscriptions

Revision ID: d4c1a7b90e21
Revises: ca79b5b80dbd
Create Date: 2026-09-08 02:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4c1a7b90e21"
down_revision: str | None = "ca79b5b80dbd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("provider_customer_id", sa.String(length=128), nullable=True),
        sa.Column("provider_subscription_id", sa.String(length=128), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_subscription_organization_id", "subscription", ["organization_id"], unique=True
    )
    op.create_index(
        "ix_subscription_provider_customer_id", "subscription", ["provider_customer_id"]
    )
    op.create_index(
        "ix_subscription_provider_subscription_id", "subscription", ["provider_subscription_id"]
    )

    op.create_table(
        "billing_event",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The delivery id is unique, which is what makes a provider's retry a
    # no-op instead of a second plan change.
    op.create_index("ix_billing_event_event_id", "billing_event", ["event_id"], unique=True)
    op.create_index("ix_billing_event_organization_id", "billing_event", ["organization_id"])

    op.create_table(
        "plan_override",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=True),
        sa.Column("tagline", sa.String(length=200), nullable=True),
        sa.Column("price_inr", sa.String(length=32), nullable=True),
        sa.Column("price_usd", sa.String(length=32), nullable=True),
        sa.Column("runs_per_month", sa.Integer(), nullable=True),
        sa.Column("flows", sa.Integer(), nullable=True),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("history_days", sa.Integer(), nullable=True),
        sa.Column(
            "features",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("product_id", sa.String(length=128), nullable=True),
        sa.Column("updated_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("code"),
    )


def downgrade() -> None:
    op.drop_table("plan_override")
    op.drop_index("ix_billing_event_organization_id", table_name="billing_event")
    op.drop_index("ix_billing_event_event_id", table_name="billing_event")
    op.drop_table("billing_event")
    op.drop_index("ix_subscription_provider_subscription_id", table_name="subscription")
    op.drop_index("ix_subscription_provider_customer_id", table_name="subscription")
    op.drop_index("ix_subscription_organization_id", table_name="subscription")
    op.drop_table("subscription")
