"""pull requests: the title and description the person edited (Job.pr_title, Job.pr_body)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-19 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pr_title", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("pr_body", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_column("pr_body")
        batch_op.drop_column("pr_title")
