"""Make tmp_providers.provider_id charset-safe for the discovery wire

Revision ID: 7c3ab19d5e02
Revises: 2fa4c97166ab
Create Date: 2026-08-18 10:00:00.000000

The pinned provider-registration schema
(``src.routes.tmp_providers.PROVIDER_REGISTRATION_SCHEMA``, resolved out of the
installed adcp SDK at ``adcp/_schemas/3.1/trusted-match/provider-registration.json``)
constrains ``provider_id`` to ``^[A-Za-z0-9_]+$`` with ``maxLength: 64`` — the
charset is deliberately safe for logs, metrics, dashboards and cache keys.

A Postgres ``uuid`` column cannot satisfy that: whatever is written, the value
is rendered back in canonical hyphenated form, and ``-`` is outside the
character class.  Every entry ``GET /tenant/{id}/tmp-providers/discovery``
returned was therefore rejected by the schema it declares conformance to
(#1197 review).

This migration converts the column to ``varchar(64)`` and strips the hyphens
from existing values, so ids stay stable (same 32 hex digits, same uniqueness)
rather than being regenerated.  The server default becomes the hyphen-free
rendering of ``gen_random_uuid()``; the ORM also assigns ``uuid4().hex``
client-side.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c3ab19d5e02"
down_revision: str | Sequence[str] | None = "2fa4c97166ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """uuid → varchar(64), hyphens stripped from existing ids."""
    op.alter_column(
        "tmp_providers",
        "provider_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=False),
        type_=sa.String(length=64),
        existing_nullable=False,
        postgresql_using="replace(provider_id::text, '-', '')",
        server_default=sa.text("replace(gen_random_uuid()::text, '-', '')"),
    )


def downgrade() -> None:
    """varchar(64) → uuid.

    Postgres parses a 32-hex-digit string as a uuid, so the ids written by the
    upgraded schema cast back cleanly. An id that is not a uuid at all (only
    reachable if something wrote one while on the new schema) fails the cast
    loudly rather than being silently dropped.
    """
    op.alter_column(
        "tmp_providers",
        "provider_id",
        existing_type=sa.String(length=64),
        type_=sa.dialects.postgresql.UUID(as_uuid=False),
        existing_nullable=False,
        postgresql_using="provider_id::uuid",
        server_default=sa.text("gen_random_uuid()"),
    )
