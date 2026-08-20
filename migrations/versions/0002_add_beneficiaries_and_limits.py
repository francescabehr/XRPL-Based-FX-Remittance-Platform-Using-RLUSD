"""Add beneficiaries and limit_tiers tables; seed default tiers

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-20

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE payoutcurrency AS ENUM ('USD', 'ZAR')")

    op.create_table(
        "beneficiaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("sender_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("mobile", sa.String(50), nullable=True),
        sa.Column("country", sa.String(100), nullable=False),
        sa.Column(
            "payout_currency",
            sa.Enum("USD", "ZAR", name="payoutcurrency", create_type=False),
            nullable=False,
        ),
        sa.Column("relationship", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_beneficiaries_sender_id", "beneficiaries", ["sender_id"])

    op.create_table(
        "limit_tiers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tier_name", sa.String(50), nullable=False),
        sa.Column("daily_limit_zar", sa.Numeric(20, 2), nullable=False),
        sa.Column("monthly_limit_zar", sa.Numeric(20, 2), nullable=False),
        sa.UniqueConstraint("tier_name", name="uq_limit_tiers_tier_name"),
    )

    # Seed default tiers — unverified users can send R0, approved users R10k/day R50k/month
    op.execute("""
        INSERT INTO limit_tiers (id, tier_name, daily_limit_zar, monthly_limit_zar)
        VALUES
            (gen_random_uuid(), 'unverified', 0.00, 0.00),
            (gen_random_uuid(), 'standard',   10000.00, 50000.00)
    """)


def downgrade() -> None:
    op.drop_table("limit_tiers")
    op.drop_table("beneficiaries")
    op.execute("DROP TYPE payoutcurrency")
