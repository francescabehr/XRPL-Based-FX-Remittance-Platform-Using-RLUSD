"""Add transactions.aml_flagged (admin AML review flag)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20

"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FR-ADM-07: an admin-only review marker. NOT NULL with a false default so
    # every existing row is unflagged; it never affects money or status.
    op.add_column(
        "transactions",
        sa.Column("aml_flagged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_transactions_aml_flagged", "transactions", ["aml_flagged"])


def downgrade() -> None:
    op.drop_index("ix_transactions_aml_flagged", table_name="transactions")
    op.drop_column("transactions", "aml_flagged")
