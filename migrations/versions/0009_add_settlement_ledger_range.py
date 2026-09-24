"""Add the settlement reliable-submission recovery pair

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-22

"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FR-MQ-06: the ledger range a signed settlement payment can appear in, the
    # same pair cash_out_requests already carries (migration 0006). Without both,
    # a payment the node cannot currently see is merely absent, not proven dead —
    # and an admin retry would re-send a payment that may already have been paid.
    # Nullable: rows settled before this migration have no recorded range, and a
    # retry on those refuses rather than guessing.
    op.add_column(
        "transactions",
        sa.Column("settlement_last_ledger_sequence", sa.Integer(), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("settlement_submitted_ledger_index", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transactions", "settlement_submitted_ledger_index")
    op.drop_column("transactions", "settlement_last_ledger_sequence")
