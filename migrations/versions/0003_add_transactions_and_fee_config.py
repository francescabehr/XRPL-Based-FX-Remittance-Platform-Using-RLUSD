"""Add transactions and fee_config tables; seed default fee config

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fee_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("fixed_fee_zar", sa.Numeric(20, 2), nullable=False),
        sa.Column("percentage_fee", sa.Numeric(10, 6), nullable=False),
        sa.Column("fx_margin", sa.Numeric(10, 6), nullable=False),
        sa.Column("cashout_fee_percentage", sa.Numeric(10, 6), nullable=False),
        sa.Column("cashout_fee_min_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("market_rate_zar_per_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # PLACEHOLDER starting values, pending group confirmation. Admin-configurable
    # via the fee & limit config screen (FR-ADM-06); changing them affects new
    # quotes only, since each transaction snapshots the values it used.
    op.execute("""
        INSERT INTO fee_config (
            id, fixed_fee_zar, percentage_fee, fx_margin,
            cashout_fee_percentage, cashout_fee_min_usd,
            market_rate_zar_per_usd, is_active
        ) VALUES (
            gen_random_uuid(), 25.00, 0.015000, 0.020000,
            0.010000, 1.000000,
            18.500000, true
        )
    """)

    op.execute("CREATE TYPE cashinstatus AS ENUM ('pending', 'received', 'failed')")
    op.execute(
        "CREATE TYPE settlementstatus AS ENUM "
        "('not_queued', 'queued', 'processing', 'completed', 'failed')"
    )

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("sender_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "beneficiary_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("beneficiaries.id"),
            nullable=False,
        ),
        sa.Column("zar_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("transaction_fee", sa.Numeric(20, 2), nullable=False),
        sa.Column("net_zar_converted", sa.Numeric(20, 2), nullable=False),
        sa.Column("market_rate", sa.Numeric(20, 6), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(20, 6), nullable=False),
        sa.Column("fx_margin", sa.Numeric(10, 6), nullable=False),
        sa.Column("uctusd_amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "cashin_status",
            postgresql.ENUM("pending", "received", "failed", name="cashinstatus", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "settlement_status",
            postgresql.ENUM(
                "not_queued", "queued", "processing", "completed", "failed",
                name="settlementstatus", create_type=False,
            ),
            nullable=False,
            server_default="not_queued",
        ),
        sa.Column("xrpl_tx_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("idempotency_key", name="uq_transactions_idempotency_key"),
    )
    op.create_index("ix_transactions_sender_id", "transactions", ["sender_id"])
    op.create_index("ix_transactions_beneficiary_id", "transactions", ["beneficiary_id"])
    # Daily/monthly usage sums filter on sender + created_at (FR-LIM-01..02).
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])


def downgrade() -> None:
    op.drop_table("transactions")
    op.execute("DROP TYPE settlementstatus")
    op.execute("DROP TYPE cashinstatus")
    op.drop_table("fee_config")
