#!/usr/bin/env python3
"""
seed_salesagent.py — Seed our fork-specific salesagent reference data.

Inserts tenants, products, pricing_options, authorized_properties,
publisher_partners, and tmp_providers that are specific to our fork.
Also registers the salesagent as a seller-agent on the tmp-provider
(T7 — Bidirectional Agent Auth).
All statements use ON CONFLICT DO NOTHING — fully idempotent.

Required environment variables (no defaults — caller must set them):
  DATABASE_URL              PostgreSQL connection string for the salesagent DB.
  TMP_PROVIDER_ENDPOINT     Internal URL of the tmp-provider service.

Optional environment variables:
  TMP_PROVIDER_ADMIN_KEY    Admin key for the tmp-provider seller-agent
                            registration API (POST /seller-agents/register).
                            When unset, the registration step is skipped
                            (tmp-provider running in open/dev mode).

The salesagent schema must already exist (alembic runs at startup).

Usage (local — via `make local-seed-salesagent`):
  Runs automatically inside the salesagent container:
  python /app/scripts/seed/seed_salesagent.py

Usage (CI — Cloud Run Job via seed:dev GitLab job):
  gcloud run jobs execute seed-salesagent-<env> --wait
"""

import os
import sys

import psycopg2

# ---------------------------------------------------------------------------
# Required env vars — fail fast if missing
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
TMP_PROVIDER_ENDPOINT = os.environ.get("TMP_PROVIDER_ENDPOINT")
TMP_PROVIDER_ADMIN_KEY = os.environ.get("TMP_PROVIDER_ADMIN_KEY", "")
# Optional: caller-supplied API key to register with tmp-provider.
# When set, the seed script passes it in the registration body so the same
# key can be used by seed_tmp_provider.sh without any write-back to GitLab.
TMP_PROVIDER_SEED_API_KEY = os.environ.get("TMP_PROVIDER_SEED_API_KEY", "")

missing = [v for v, val in [("DATABASE_URL", DATABASE_URL), ("TMP_PROVIDER_ENDPOINT", TMP_PROVIDER_ENDPOINT)] if not val]
if missing:
    print(f"❌ Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# HTTP helper (for tmp-provider seller-agent registration)
# ---------------------------------------------------------------------------

def register_seller_agent():
    """Register the salesagent as a seller-agent on the tmp-provider.

    Calls POST {TMP_PROVIDER_ENDPOINT}/seller-agents/register.
    Idempotent: a 409 Conflict response means the agent is already
    registered — treated as success.

    When TMP_PROVIDER_ADMIN_KEY is set it is sent as the Bearer token to
    satisfy the admin-key guard on the endpoint.  When unset the endpoint
    is assumed to be open (no TMP_PROVIDER_ADMIN_KEY configured on the
    server) and the request is sent without an Authorization header.

    When TMP_PROVIDER_SEED_API_KEY is set it is included in the request
    body as "api_key" so the server stores its hash directly.  This makes
    the seed fully idempotent without any write-back to GitLab: the same
    CI variable is used for both registration and subsequent /packages/sync
    calls.  When unset the server generates a random key (shown once in
    the job log).
    """
    import urllib.request
    import urllib.error
    import json as _json

    url = f"{TMP_PROVIDER_ENDPOINT.rstrip('/')}/seller-agents/register"
    body_dict = {
        "agent_url": TMP_PROVIDER_ENDPOINT.rstrip("/"),
        "tenant_id": "siteplug",
        "display_name": "tmp-provider-demo",
    }
    if TMP_PROVIDER_SEED_API_KEY:
        body_dict["api_key"] = TMP_PROVIDER_SEED_API_KEY

    payload = _json.dumps(body_dict).encode()

    headers = {"Content-Type": "application/json"}
    if TMP_PROVIDER_ADMIN_KEY:
        headers["Authorization"] = f"Bearer {TMP_PROVIDER_ADMIN_KEY}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print(f"  ✓ seller-agent registered on tmp-provider (HTTP {resp.status})")
            data = _json.loads(body)
            if data.get("api_key") and not TMP_PROVIDER_SEED_API_KEY:
                print(f"    api_key (shown once): {data['api_key']}")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print("  ✓ seller-agent already registered on tmp-provider — skipping")
        else:
            body = e.read().decode()
            print(f"  ⚠️  seller-agent registration returned HTTP {e.code}: {body}", file=sys.stderr)
    except Exception as exc:
        print(f"  ⚠️  seller-agent registration failed (tmp-provider unreachable?): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_conn():
    """Return a psycopg2 connection. Strips SQLAlchemy driver prefix if present."""
    url = DATABASE_URL
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


def run_sql(conn, sql: str, label: str = "") -> None:
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.close()
    if label:
        print(f"  ✓ {label}")


def count(conn, sql: str) -> int:
    cur = conn.cursor()
    cur.execute(sql)
    result = cur.fetchone()
    cur.close()
    return int(result[0]) if result else 0


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

TENANTS = [
    # (tenant_id, name, subdomain, adapter, admin_token, principal_token)
    ("mcanvas",      "mCanvas",      "mcanvas",      "mock",     "mcanvas-admin-token",      "mcanvas-token"),
    ("veve",         "Veve",         "veve",         "mock",     "veve-admin-token",         "veve-token"),
    ("siteplug",     "SitePlug",     "siteplug",     "siteplug", "siteplug-admin-token",     "siteplug-token"),
    # Storyboard compliance test tenant — acme-outdoor test kit (AdCP 3.0 media_buy_seller)
    ("acme-outdoor", "Acme Outdoor", "acme-outdoor", "mock",     "acme-outdoor-admin-token", "acme-outdoor-token"),
]


def seed_tenant(conn, tenant_id, name, subdomain, adapter, admin_token, principal_token):
    n = count(conn, f"SELECT COUNT(*) FROM tenants WHERE tenant_id='{tenant_id}'")
    if n > 0:
        print(f"  ✓ Tenant '{tenant_id}' already exists — skipping")
        return

    print(f"  Creating tenant '{tenant_id}' ({name})...")
    run_sql(conn, f"""
        INSERT INTO tenants
          (tenant_id, name, subdomain, is_active, billing_plan, ad_server,
           enable_axe_signals, admin_token, human_review_required,
           auto_approve_format_ids, brand_manifest_policy,
           authorized_domains,
           created_at, updated_at)
        VALUES
          ('{tenant_id}', '{name}', '{subdomain}', true, 'standard', '{adapter}',
           true, '{admin_token}', true,
           '["display_300x250","display_728x90","display_320x50"]'::jsonb,
           'public', '["affinity.com"]'::jsonb,
           NOW(), NOW())
        ON CONFLICT (tenant_id) DO NOTHING
    """)
    run_sql(conn, f"""
        INSERT INTO adapter_config (tenant_id, adapter_type, created_at, updated_at)
        VALUES ('{tenant_id}', '{adapter}', NOW(), NOW())
        ON CONFLICT (tenant_id) DO NOTHING
    """)
    run_sql(conn, f"""
        INSERT INTO principals
          (tenant_id, principal_id, name, platform_mappings, access_token,
           created_at, updated_at)
        VALUES
          ('{tenant_id}', '{tenant_id}_principal', '{name} Principal',
           '{{"mock": {{"advertiser_id": "mock-{tenant_id}"}}}}'::jsonb,
           '{principal_token}', NOW(), NOW())
        ON CONFLICT (tenant_id, principal_id) DO NOTHING
    """)
    print(f"  ✓ Tenant '{tenant_id}' created (token: {principal_token})")


def migrate_siteplug_adapter(conn):
    """Migrate siteplug tenant from mock → siteplug adapter if stale."""
    cur = conn.cursor()
    cur.execute("SELECT adapter_type FROM adapter_config WHERE tenant_id='siteplug' LIMIT 1")
    row = cur.fetchone()
    cur.close()
    if row and row[0] == "mock":
        print("  ⚠️  Migrating siteplug adapter: mock → siteplug")
        run_sql(conn, """
            UPDATE adapter_config SET adapter_type = 'siteplug', updated_at = NOW()
            WHERE tenant_id = 'siteplug' AND adapter_type = 'mock'
        """)
        run_sql(conn, """
            UPDATE tenants SET ad_server = 'siteplug', updated_at = NOW()
            WHERE tenant_id = 'siteplug' AND ad_server = 'mock'
        """, "siteplug adapter migrated")


def seed_products(conn, tenant_id, label):
    n = count(conn, f"SELECT COUNT(*) FROM products WHERE tenant_id='{tenant_id}'")
    if n > 0:
        print(f"  ✓ {label} already has {n} product(s) — skipping")
        return

    print(f"  Seeding products for {label}...")
    run_sql(conn, f"""
        INSERT INTO products
          (tenant_id, product_id, name, description,
           format_ids, targeting_template, delivery_type,
           price_guidance, property_tags)
        VALUES
          (
            '{tenant_id}', '{tenant_id}_display_premium',
            '{label} Premium Display',
            'Premium display advertising — 300x250 and 728x90 across all sections',
            '[
              {{"agent_url": "https://creative.adcontextprotocol.org", "id": "display_300x250"}},
              {{"agent_url": "https://creative.adcontextprotocol.org", "id": "display_728x90"}}
            ]'::jsonb,
            '{{"geo_countries": ["US", "CA", "GB"]}}'::jsonb,
            'guaranteed',
            '{{"floor": 5.0, "p50": 10.0, "p75": 15.0}}'::jsonb,
            '["all_inventory"]'::jsonb
          ),
          (
            '{tenant_id}', '{tenant_id}_video_preroll',
            '{label} Video Pre-roll',
            'Pre-roll video ads — 15s and 30s spots',
            '[
              {{"agent_url": "https://creative.adcontextprotocol.org", "id": "video_preroll", "duration_ms": 15000}},
              {{"agent_url": "https://creative.adcontextprotocol.org", "id": "video_preroll", "duration_ms": 30000}}
            ]'::jsonb,
            '{{"geo_countries": ["US"]}}'::jsonb,
            'guaranteed',
            '{{"floor": 15.0, "p50": 22.0, "p75": 30.0}}'::jsonb,
            '["all_inventory"]'::jsonb
          ),
          (
            '{tenant_id}', '{tenant_id}_ros_display',
            '{label} Run-of-Site Display',
            'Run-of-site display inventory — non-guaranteed, broad reach',
            '[
              {{"agent_url": "https://creative.adcontextprotocol.org", "id": "display_300x250"}}
            ]'::jsonb,
            '{{}}'::jsonb,
            'non_guaranteed',
            '{{"floor": 1.5, "p50": 3.0, "p75": 5.0}}'::jsonb,
            '["all_inventory"]'::jsonb
          )
        ON CONFLICT (tenant_id, product_id) DO NOTHING
    """, f"{label} products seeded (3 products)")


def seed_pricing_options(conn, tenant_id, label):
    n = count(conn, f"SELECT COUNT(*) FROM pricing_options WHERE tenant_id='{tenant_id}'")
    if n > 0:
        print(f"  ✓ {label} already has {n} pricing option(s) — skipping")
        return

    print(f"  Seeding pricing_options for {label}...")
    run_sql(conn, f"""
        INSERT INTO pricing_options
          (tenant_id, product_id, pricing_model, rate, currency, is_fixed,
           price_guidance, parameters, min_spend_per_package)
        VALUES
          ('{tenant_id}', '{tenant_id}_display_premium', 'cpm', 5.00,  'USD', true,
           '{{"floor": 5.0, "p50": 10.0, "p75": 15.0}}'::jsonb, NULL, 500.00),
          ('{tenant_id}', '{tenant_id}_display_premium', 'cpm', NULL,  'USD', false,
           '{{"floor": 5.0, "p50": 10.0, "p75": 15.0}}'::jsonb, NULL, 250.00),
          ('{tenant_id}', '{tenant_id}_video_preroll',   'cpm', 15.00, 'USD', true,
           '{{"floor": 15.0, "p50": 22.0, "p75": 30.0}}'::jsonb, NULL, 1000.00),
          ('{tenant_id}', '{tenant_id}_video_preroll',   'cpcv', 0.05, 'USD', true,
           '{{"floor": 0.05, "p50": 0.08, "p75": 0.12}}'::jsonb, NULL, 500.00),
          ('{tenant_id}', '{tenant_id}_ros_display',     'cpm', NULL,  'USD', false,
           '{{"floor": 1.5, "p50": 3.0, "p75": 5.0}}'::jsonb, NULL, 100.00)
        ON CONFLICT DO NOTHING
    """, f"{label} pricing_options seeded (5 options)")


def seed_currency_limits(conn, tenant_id, label):
    """Seed default currency limits (USD, EUR, GBP) for a tenant.

    Required for create_media_buy — the salesagent validates that at least one
    currency is configured before accepting a media buy request.  The alembic
    migration 9309ac2fa74f adds these for tenants that existed at migration time,
    but tenants created afterwards (via this seed script) are not covered.
    """
    n = count(conn, f"SELECT COUNT(*) FROM currency_limits WHERE tenant_id='{tenant_id}'")
    if n > 0:
        print(f"  ✓ {label} already has {n} currency limit(s) — skipping")
        return

    print(f"  Seeding currency_limits for {label}...")
    run_sql(conn, f"""
        INSERT INTO currency_limits
          (tenant_id, currency_code, min_package_budget, max_daily_package_spend,
           created_at, updated_at)
        VALUES
          ('{tenant_id}', 'USD', 0.00, 100000.00, NOW(), NOW()),
          ('{tenant_id}', 'EUR', 0.00, 100000.00, NOW(), NOW()),
          ('{tenant_id}', 'GBP', 0.00, 100000.00, NOW(), NOW())
        ON CONFLICT (tenant_id, currency_code) DO NOTHING
    """, f"{label} currency_limits seeded (USD, EUR, GBP — no minimum, $100k daily max)")


def seed_authorized_properties(conn, tenant_id, label):
    domain = f"{tenant_id}.example.com"

    ap_n = count(conn, f"SELECT COUNT(*) FROM authorized_properties WHERE tenant_id='{tenant_id}'")
    if ap_n > 0:
        print(f"  ✓ {label} already has {ap_n} authorized_propert(ies) — skipping")
    else:
        print(f"  Seeding authorized_properties for {label}...")
        run_sql(conn, f"""
            INSERT INTO authorized_properties
              (property_id, tenant_id, name, publisher_domain, property_type,
               identifiers, verification_status, created_at, updated_at)
            VALUES
              (
                '{tenant_id}_example_com', '{tenant_id}',
                '{label} Example Property', '{domain}', 'website',
                '[{{"type": "domain", "value": "{domain}"}}]'::jsonb,
                'verified', NOW(), NOW()
              )
            ON CONFLICT DO NOTHING
        """, f"{label} authorized_properties seeded")

    pp_n = count(conn, f"SELECT COUNT(*) FROM publisher_partners WHERE tenant_id='{tenant_id}' AND is_verified=true")
    if pp_n > 0:
        print(f"  ✓ {label} already has {pp_n} verified publisher_partner(s) — skipping")
    else:
        print(f"  Seeding publisher_partners for {label}...")
        run_sql(conn, f"""
            INSERT INTO publisher_partners
              (tenant_id, publisher_domain, display_name, is_verified, sync_status,
               created_at, updated_at)
            VALUES
              ('{tenant_id}', '{domain}', '{label} Publisher', true, 'success', NOW(), NOW())
            ON CONFLICT (tenant_id, publisher_domain) DO UPDATE
              SET is_verified = true, sync_status = 'success', updated_at = NOW()
        """, f"{label} publisher_partners seeded")


def seed_tmp_provider(conn):
    n = count(conn, "SELECT COUNT(*) FROM tmp_providers WHERE tenant_id='siteplug'")
    if n > 0:
        print(f"  ✓ siteplug already has {n} tmp_provider(s) — skipping")
        return

    print(f"  Seeding tmp_providers for siteplug (endpoint: {TMP_PROVIDER_ENDPOINT})...")
    run_sql(conn, f"""
        INSERT INTO tmp_providers
          (tenant_id, name, endpoint, context_match, identity_match,
           countries, uid_types, priority, status,
           timeout_ms, created_at, updated_at)
        VALUES
          (
            'siteplug', 'tmp-provider-demo', '{TMP_PROVIDER_ENDPOINT}',
            true, true,
            '["US"]'::jsonb,
            '["publisher_first_party","uid2","hashed_email"]'::jsonb,
            0, 'active', 200, NOW(), NOW()
          )
        ON CONFLICT DO NOTHING
    """, f"tmp_providers seeded (tmp-provider-demo → {TMP_PROVIDER_ENDPOINT})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  seed_salesagent.py: Fork-specific reference data")
    print("=" * 60)
    print()

    conn = get_conn()

    # Verify schema is ready (alembic must have run first)
    try:
        count(conn, "SELECT COUNT(*) FROM tenants LIMIT 1")
    except Exception as e:
        print(f"❌ salesagent schema not ready: {e}", file=sys.stderr)
        print("   Ensure alembic migrations have completed before seeding.", file=sys.stderr)
        sys.exit(1)

    print("Step 1: Seeding core tenants...")
    for t in TENANTS:
        seed_tenant(conn, *t)
    migrate_siteplug_adapter(conn)
    print()

    print("Step 2: Seeding products...")
    for tenant_id, name, *_ in TENANTS:
        seed_products(conn, tenant_id, name)
    print()

    print("Step 3: Seeding pricing_options...")
    for tenant_id, name, *_ in TENANTS:
        seed_pricing_options(conn, tenant_id, name)
    print()

    print("Step 4: Seeding authorized_properties + publisher_partners...")
    for tenant_id, name, *_ in TENANTS:
        seed_authorized_properties(conn, tenant_id, name)
    print()

    print("Step 5: Seeding currency_limits...")
    for tenant_id, name, *_ in TENANTS:
        seed_currency_limits(conn, tenant_id, name)
    print()

    print("Step 6: Seeding tmp_providers for siteplug...")
    seed_tmp_provider(conn)
    print()

    print("Step 7: Registering salesagent as seller-agent on tmp-provider (T7)...")
    register_seller_agent()
    print()

    print("Step 8: Verification...")
    for tenant_id, name, *_ in TENANTS:
        prod_n     = count(conn, f"SELECT COUNT(*) FROM products WHERE tenant_id='{tenant_id}'")
        pricing_n  = count(conn, f"SELECT COUNT(*) FROM pricing_options WHERE tenant_id='{tenant_id}'")
        ap_n       = count(conn, f"SELECT COUNT(*) FROM authorized_properties WHERE tenant_id='{tenant_id}'")
        pp_n       = count(conn, f"SELECT COUNT(*) FROM publisher_partners WHERE tenant_id='{tenant_id}'")
        currency_n = count(conn, f"SELECT COUNT(*) FROM currency_limits WHERE tenant_id='{tenant_id}'")
        print(f"  {tenant_id}: {prod_n} products, {pricing_n} pricing, {ap_n} auth props, {pp_n} partners, {currency_n} currencies")
    tmp_n = count(conn, "SELECT COUNT(*) FROM tmp_providers WHERE tenant_id='siteplug' AND status='active'")
    print(f"  siteplug: {tmp_n} active TMP provider(s)")

    conn.close()

    print()
    print("=" * 60)
    print("  ✅ salesagent seed complete!")
    print("=" * 60)
    print()
    print("  Tenants: mcanvas, veve, siteplug, acme-outdoor")
    print("  Each tenant seeded with: products, pricing, auth props, publisher partners,")
    print("  currency limits (USD/EUR/GBP — required for create_media_buy)")
    print("  Well-known tokens (dev/staging only):")
    for tenant_id, _, __, ___, ____, token in TENANTS:
        print(f"    {tenant_id}: {token}")
    print()


if __name__ == "__main__":
    main()
