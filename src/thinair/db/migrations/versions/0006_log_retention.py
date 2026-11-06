"""log retention: Job.logs_removed_at

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("logs_removed_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_column("logs_removed_at")
