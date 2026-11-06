"""runners: Runner table and Job.runner_id

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06 11:47:07.551075
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runner",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("info", sa.JSON(), nullable=True),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.Column("disabled_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("runner", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runner_name"), ["name"], unique=True)
        batch_op.create_index(batch_op.f("ix_runner_token_hash"), ["token_hash"], unique=True)

    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("runner_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.create_index(batch_op.f("ix_job_runner_id"), ["runner_id"], unique=False)
        batch_op.create_foreign_key("fk_job_runner_id", "runner", ["runner_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_constraint("fk_job_runner_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_job_runner_id"))
        batch_op.drop_column("runner_id")

    with op.batch_alter_table("runner", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runner_token_hash"))
        batch_op.drop_index(batch_op.f("ix_runner_name"))

    op.drop_table("runner")
