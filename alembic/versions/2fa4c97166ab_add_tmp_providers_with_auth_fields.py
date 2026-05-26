"""Add tmp_providers table with auth fields for Trusted Match Protocol provider registrations

Revision ID: 2fa4c97166ab
Revises: b4e2bffdd4f8
Create Date: 2026-05-21 09:31:00.000000

Schema aligned with provider-registration.json (AdCP spec PR #2210):
  - status string (active/inactive/draining) instead of is_active boolean
  - countries (JSONB, conditional on identity_match)
  - uid_types (JSONB, conditional on identity_match)
  - properties (JSONB, optional property RIDs)
  - priority (integer, default 0)
  - auth_type (string, e.g. "bearer", "api_key") — nullable
  - auth_credentials (text, stores token/key value) — nullable

TMP Provider sync always uses the standard Authorization: Bearer header,
so auth_header is intentionally omitted (unlike CreativeAgent/SignalsAgent).
Both auth columns are nullable — existing rows have no auth configured.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2fa4c97166ab"
down_revision: str | Sequence[str] | None = "b4e2bffdd4f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add auth_type and auth_credentials columns to existing tmp_providers table.

    The tmp_providers table was created by migration 20260413120000 without auth fields.
    This migration adds the two auth columns introduced in the auth-fields revision.
    Uses IF NOT EXISTS guards so it is safe to run even if columns already exist
    (e.g. on a fresh DB where both migrations run in sequence via a merge head).
    """
    conn = op.get_bind()

    # Add auth_type column if it doesn't already exist
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='tmp_providers' AND column_name='auth_type'"
        )
    )
    if result.fetchone() is None:
        op.add_column("tmp_providers", sa.Column("auth_type", sa.String(length=50), nullable=True))

    # Add auth_credentials column if it doesn't already exist
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='tmp_providers' AND column_name='auth_credentials'"
        )
    )
    if result.fetchone() is None:
        op.add_column("tmp_providers", sa.Column("auth_credentials", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove auth columns added by this migration."""
    op.drop_column("tmp_providers", "auth_credentials")
    op.drop_column("tmp_providers", "auth_type")
