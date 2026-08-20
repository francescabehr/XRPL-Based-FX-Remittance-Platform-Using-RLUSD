"""Init users and KYC tables

Revision ID: 0001
Revises:
Create Date: 2026-08-20

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE kycstatus AS ENUM ('not_submitted', 'pending', 'approved', 'rejected')")
    op.execute("CREATE TYPE kycsubmissionstatus AS ENUM ('pending', 'approved', 'rejected')")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("mobile", sa.String(50), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("can_send", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("can_receive", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "kyc_status",
            sa.Enum("not_submitted", "pending", "approved", "rejected", name="kycstatus", create_type=False),
            nullable=False,
            server_default="not_submitted",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("mobile", name="uq_users_mobile"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "kyc_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("nationality", sa.String(100), nullable=False),
        sa.Column("id_number", sa.String(100), nullable=False),
        sa.Column("residential_address", sa.Text(), nullable=False),
        sa.Column("mobile", sa.String(50), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("source_of_funds", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="kycsubmissionstatus", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_kyc_submissions_user_id", "kyc_submissions", ["user_id"])
    op.create_index("ix_kyc_submissions_status", "kyc_submissions", ["status"])


def downgrade() -> None:
    op.drop_table("kyc_submissions")
    op.drop_table("users")
    op.execute("DROP TYPE kycsubmissionstatus")
    op.execute("DROP TYPE kycstatus")
