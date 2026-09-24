"""Add fee_config.min_send_zar (minimum remittance amount)

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-22

"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FR-FX-02/04: below this, the transaction fee consumes the whole send and the
    # quote produces a zero or negative UCTUSD amount — an amount the ledger can
    # never carry. PLACEHOLDER value, pending group confirmation, in the style of
    # the other fee_config defaults; admin-configurable via /admin/config.
    # At the seeded config (R25 fixed + 1.5%) the break-even is R25.39, so R50
    # leaves the recipient a usable amount rather than sitting on the edge.
    op.add_column(
        "fee_config",
        sa.Column(
            "min_send_zar",
            sa.Numeric(20, 2),
            nullable=False,
            server_default=sa.text("50.00"),
        ),
    )


def downgrade() -> None:
    op.drop_column("fee_config", "min_send_zar")
