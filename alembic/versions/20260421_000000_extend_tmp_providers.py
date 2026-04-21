"""Extend tmp_providers with countries, uid_types, priority, status columns

Revision ID: 20260421000000
Revises: 20260413120000
Create Date: 2026-04-21 00:00:00.000000

Adds the fields required by the TMP Router discovery endpoint:
  - countries   JSONB  — ISO 3166-1 alpha-2 codes the provider serves
  - uid_types   JSONB  — identity token types the provider accepts
  - priority    INT    — lower = higher priority (default 0)
  - status      TEXT   — 'active' | 'inactive' | 'draining' (default 'active')

The existing is_active boolean is kept for backward compatibility with the
admin UI; the new status column is the authoritative lifecycle field used by
the discovery endpoint.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260421000000"
down_revision: str | Sequence[str] | None = "20260413120000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add countries, uid_types, priority, status to tmp_providers."""
    # countries — JSONB array of ISO 3166-1 alpha-2 strings, e.g. ["US", "GB"]
    op.add_column(
        "tmp_providers",
        sa.Column(
            "countries",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="ISO 3166-1 alpha-2 country codes this provider serves (null = all countries)",
        ),
    )

    # uid_types — JSONB array of identity token type strings
    op.add_column(
        "tmp_providers",
        sa.Column(
            "uid_types",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Identity token types this provider accepts (null = all types)",
        ),
    )

    # priority — integer, lower value = higher priority, default 0
    op.add_column(
        "tmp_providers",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Fan-out priority: lower integer = higher priority",
        ),
    )

    # status — lifecycle state enum as text with check constraint
    op.add_column(
        "tmp_providers",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'active'"),
            comment="Provider lifecycle: active | inactive | draining",
        ),
    )

    op.create_check_constraint(
        "ck_tmp_providers_status",
        "tmp_providers",
        "status IN ('active', 'inactive', 'draining')",
    )

    # Index on status for fast filtering in the discovery query
    op.create_index("idx_tmp_providers_status", "tmp_providers", ["status"])


def downgrade() -> None:
    """Remove the new columns from tmp_providers."""
    op.drop_index("idx_tmp_providers_status", table_name="tmp_providers")
    op.drop_constraint("ck_tmp_providers_status", "tmp_providers", type_="check")
    op.drop_column("tmp_providers", "status")
    op.drop_column("tmp_providers", "priority")
    op.drop_column("tmp_providers", "uid_types")
    op.drop_column("tmp_providers", "countries")
