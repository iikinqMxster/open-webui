"""Add subagent table

Revision ID: b7f3a1c9d2e4
Revises: 55f1302ac17c
Create Date: 2026-07-24 00:00:00.000000

Creates the workspace `subagent` table. `id` is an internal UUID that models bind to
(meta.subagentIds); `handle` is the user-put, editable id (e.g. "math-agent") the lead
agent references. Guarded so it is safe whether the table is absent, or already exists
from an earlier attempt (in which case only a missing `handle` column is added).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from open_webui.migrations.util import get_existing_tables

revision: str = 'b7f3a1c9d2e4'
down_revision: Union[str, None] = '55f1302ac17c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if 'subagent' not in set(get_existing_tables()):
        op.create_table(
            'subagent',
            sa.Column('id', sa.String(), nullable=False, primary_key=True),
            sa.Column('user_id', sa.String(), nullable=False),
            sa.Column('handle', sa.String(), nullable=True, unique=True),
            sa.Column('name', sa.Text(), nullable=False, unique=True),
            sa.Column('base_model_id', sa.Text(), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('params', sa.JSON(), nullable=True),
            sa.Column('meta', sa.JSON(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
        )
        op.create_index('idx_subagent_user_id', 'subagent', ['user_id'])
        op.create_index('idx_subagent_updated_at', 'subagent', ['updated_at'])
        op.create_index('idx_subagent_handle', 'subagent', ['handle'], unique=True)
    elif 'handle' not in _columns('subagent'):
        # Table exists from an earlier attempt without the handle column.
        op.add_column('subagent', sa.Column('handle', sa.String(), nullable=True))
        op.create_index('idx_subagent_handle', 'subagent', ['handle'], unique=True)


def downgrade() -> None:
    if 'subagent' in set(get_existing_tables()):
        op.drop_table('subagent')
