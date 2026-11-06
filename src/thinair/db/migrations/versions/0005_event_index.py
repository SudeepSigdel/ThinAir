"""event index: (job_id, seq) replaces the job_id-only index on jobevent

Every event read is "this job, after seq N, ordered by seq"; with only a job_id index SQLite
sorted all of a job's events (up to 50 000) on every live-log fetch.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobevent", schema=None) as batch_op:
        batch_op.create_index("ix_jobevent_job_id_seq", ["job_id", "seq"], unique=False)
        batch_op.drop_index(batch_op.f("ix_jobevent_job_id"))


def downgrade() -> None:
    with op.batch_alter_table("jobevent", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_jobevent_job_id"), ["job_id"], unique=False)
        batch_op.drop_index("ix_jobevent_job_id_seq")
