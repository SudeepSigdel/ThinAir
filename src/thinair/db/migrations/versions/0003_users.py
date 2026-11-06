"""users: User table, ApiToken.user_id and Job.user_id

No data migration: a NULL user_id means the owner (the original single admin), so existing tokens
and jobs keep working unchanged.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06 09:38:29.741129
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column("disabled_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_name"), ["name"], unique=True)

    for table in ("apitoken", "job"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
            )
            batch_op.create_index(batch_op.f(f"ix_{table}_user_id"), ["user_id"], unique=False)
            batch_op.create_foreign_key(f"fk_{table}_user_id", "user", ["user_id"], ["id"])


def downgrade() -> None:
    for table in ("job", "apitoken"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(f"fk_{table}_user_id", type_="foreignkey")
            batch_op.drop_index(batch_op.f(f"ix_{table}_user_id"))
            batch_op.drop_column("user_id")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_name"))

    op.drop_table("user")
