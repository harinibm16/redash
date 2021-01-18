"""Add user credentials JSON column

Revision ID: fe59d88215bb
Revises: e5c7a4e2df4d
Create Date: 2021-01-05 08:23:33.460771

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'fe59d88215bb'
down_revision = 'e5c7a4e2df4d'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('credentials', postgresql.JSON(astext_type=sa.Text()), server_default='{}', nullable=True))


def downgrade():
    op.drop_column('users', 'credentials')
