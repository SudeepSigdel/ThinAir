"""job limits and pairing: User.max_active_jobs and the pairingcode table

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pairingcode",
        sa.Column("code_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("expires_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("used_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_pairingcode_user_id"),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("max_active_jobs", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("max_active_jobs")
    op.drop_table("pairingcode")
