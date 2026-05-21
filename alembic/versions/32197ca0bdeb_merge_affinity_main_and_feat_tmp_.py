"""merge affinity-main and feat/tmp-integration heads

Revision ID: 32197ca0bdeb
Revises: 2fa4c97166ab, 46d5d2ac70b0
Create Date: 2026-05-21 13:14:47.993055

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '32197ca0bdeb'
down_revision: Union[str, Sequence[str], None] = ('2fa4c97166ab', '46d5d2ac70b0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
