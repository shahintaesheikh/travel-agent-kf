"""Alembic migration script template."""
# type: ignore[empty-body,import-untyped]

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = ...
down_revision: Union[str, None] = ...
branch_labels: Union[str, Sequence[str], None] = ...
depends_on: Union[str, Sequence[str], None] = ...


def upgrade() -> None:
    ...


def downgrade() -> None:
    ...