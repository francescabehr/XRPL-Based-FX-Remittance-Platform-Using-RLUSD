"""Add cashout_requests table (recipient UCTUSD -> fiat, burned on-ledger)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # payoutcurrency already exists from 0002 and is reused verbatim (USD, ZAR).
    op.execute("CREATE TYPE cashoutstatus AS ENUM ('requested', 'approved', 'completed', 'failed')")
    op.create_table(
        "cashout_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("uctusd_amount", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "target_currency",
            postgresql.ENUM("USD", "ZAR", name="payoutcurrency", create_type=False),
            nullable=False,
        ),
        # Pricing snapshot (FR-CO-02) — approval honours these, never a recompute.
        sa.Column("market_rate", sa.Numeric(20, 6), nullable=False),
        sa.Column("cashout_fee_percentage", sa.Numeric(10, 6), nullable=False),
        sa.Column("cashout_fee_min_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("cashout_fee_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("net_payout", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "requested", "approved", "completed", "failed",
                name="cashoutstatus", create_type=False,
            ),
            nullable=False,
            server_default="requested",
        ),
        # Admin action (FR-CO-05).
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        # Burn + reliable-submission recovery metadata.
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("xrpl_burn_tx_hash", sa.String(64), nullable=True),
        sa.Column("burn_last_ledger_sequence", sa.Integer(), nullable=True),
        sa.Column("burn_submitted_ledger_index", sa.Integer(), nullable=True),
        sa.Column("burn_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("burn_attempts", sa.Integer(), nullable=False, server_default="0"),
        # Simulated fiat payout.
        sa.Column("fiat_payout_reference", sa.String(64), nullable=True),
        sa.Column("fiat_paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("idempotency_key", name="uq_cashout_requests_idempotency_key"),
    )
    op.create_index("ix_cashout_requests_recipient_user_id", "cashout_requests", ["recipient_user_id"])
    op.create_index("ix_cashout_requests_created_at", "cashout_requests", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_cashout_requests_created_at", table_name="cashout_requests")
    op.drop_index("ix_cashout_requests_recipient_user_id", table_name="cashout_requests")
    op.drop_table("cashout_requests")
    op.execute("DROP TYPE cashoutstatus")
