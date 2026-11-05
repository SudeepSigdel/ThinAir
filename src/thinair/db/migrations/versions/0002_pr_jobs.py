"""pr jobs: Job.parent_job_id, JobResult.patch and .pr_url

The new JobKind value "PR" needs no schema change on SQLite: the enum is stored as a VARCHAR
without a CHECK constraint. On Postgres the enum type gets the new value (not removed on downgrade).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06 00:09:22.329691
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # SQLAlchemy stores enum *names*; Postgres keeps them in a real enum type.
        op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'PR'")
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("parent_job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.create_index(batch_op.f("ix_job_parent_job_id"), ["parent_job_id"], unique=False)
        batch_op.create_foreign_key("fk_job_parent_job_id", "job", ["parent_job_id"], ["id"])

    with op.batch_alter_table("jobresult", schema=None) as batch_op:
        batch_op.add_column(sa.Column("patch", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("pr_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobresult", schema=None) as batch_op:
        batch_op.drop_column("pr_url")
        batch_op.drop_column("patch")

    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_constraint("fk_job_parent_job_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_job_parent_job_id"))
        batch_op.drop_column("parent_job_id")
