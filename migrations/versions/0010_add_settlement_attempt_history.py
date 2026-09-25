"""Add transactions.settlement_previous_attempts (retired settlement attempts)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-24

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FR-MQ-06: an admin retry clears xrpl_tx_hash so the row can be claimed and
    # signed again. That used to destroy the only record of a payment that may
    # exist on-ledger — exactly what is needed to investigate it later.
    #
    # Each entry archives the attempt as a whole: tx_hash together with the
    # ledger range it could appear in (migration 0009). The hash alone would not
    # be enough — without its range, a missing transaction cannot be told apart
    # from one the node cannot see, which is the very problem 0009 fixed.
    op.add_column(
        "transactions",
        sa.Column(
            "settlement_previous_attempts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("transactions", "settlement_previous_attempts")
