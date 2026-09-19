"""Add cash-in and settlement tracking fields to transactions

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-19

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_transactions_recipient_user_id", "transactions", ["recipient_user_id"])
    op.add_column("transactions", sa.Column("card_last4", sa.String(4), nullable=True))
    op.add_column("transactions", sa.Column("cashin_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "transactions",
        sa.Column("cashin_reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column("transactions", sa.Column("cashin_failure_reason", sa.Text(), nullable=True))
    op.add_column(
        "transactions",
        sa.Column("settlement_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("transactions", sa.Column("xrpl_error_reason", sa.Text(), nullable=True))
    op.add_column("transactions", sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("transactions", "settled_at")
    op.drop_column("transactions", "xrpl_error_reason")
    op.drop_column("transactions", "settlement_attempts")
    op.drop_column("transactions", "cashin_failure_reason")
    op.drop_column("transactions", "cashin_reviewed_by")
    op.drop_column("transactions", "cashin_updated_at")
    op.drop_column("transactions", "card_last4")
    op.drop_index("ix_transactions_recipient_user_id", table_name="transactions")
    op.drop_column("transactions", "recipient_user_id")
