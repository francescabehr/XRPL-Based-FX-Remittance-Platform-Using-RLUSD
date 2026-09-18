"""Add wallets table (per-recipient XRPL accounts)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("xrpl_address", sa.String(35), nullable=False),
        sa.Column("encrypted_private_key", sa.Text(), nullable=False),
        sa.Column("key_encryption_key_id", sa.String(64), nullable=False),
        sa.Column("trust_set_complete", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("balance_uctusd", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", name="uq_wallets_user_id"),
        sa.UniqueConstraint("xrpl_address", name="uq_wallets_xrpl_address"),
    )


def downgrade() -> None:
    op.drop_table("wallets")
