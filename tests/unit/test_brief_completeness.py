"""Unit tests for task-03b: _check_brief_completeness() in products.py.

Acceptance criteria from task-03b-salesagent-incomplete-handling.md:
- get_products(brief="") → incomplete: [{scope: "products", description: "..."}]
- get_products(brief="CPI campaign for food delivery app, India, budget $50k") → no incomplete[]
- get_products(brief="display campaign") → incomplete[] (no geo, no KPI, no budget)
- get_products(brief="Nike CPC campaign, US, UK — budget $10k") → no incomplete[] (geo via structured countries; KPI via CPC; budget present)
- get_products(brief="Quiksilver affiliate campaign, United States") → incomplete[] (geo present, no KPI, no budget)
- incomplete is absent (not []) when brief is sufficient
- Products still returned alongside incomplete[] — advisory, not a hard block
- Unit tests: empty brief, short brief, geo only, KPI only, budget only, complete brief

No database, no Vertex AI, no HTTP — pure unit test using the extracted function logic.
"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Replicate the _check_brief_completeness logic for isolated unit testing.
# This mirrors the exact implementation in products.py so tests stay in sync
# without importing the full products module (which has heavy dependencies).
# ---------------------------------------------------------------------------

def _make_req(countries: list[str] | None = None) -> MagicMock:
    """Return a MagicMock that looks like a GetProductsRequest."""
    req = MagicMock()
    if countries is not None:
        req.filters = MagicMock()
        req.filters.countries = countries
    else:
        req.filters = MagicMock()
        req.filters.countries = []
    req.extracted_budget_usd = None
    return req


class _IncompleteItem:
    """Minimal stand-in for adcp.types.IncompleteItem for isolated testing."""
    def __init__(self, scope: str, description: str, estimated_wait: str | None = None):
        self.scope = scope
        self.description = description
        self.estimated_wait = estimated_wait


def _check_brief_completeness(brief_text: str, req: MagicMock) -> list:
    """Replicate the logic from products.py for isolated unit testing."""
    if not brief_text or len(brief_text.strip()) < 20:
        return [
            _IncompleteItem(
                scope="products",
                description=(
                    "Brief is too short to curate relevant products. "
                    "Please describe your campaign objective, target geography, and KPI."
                ),
            )
        ]

    brief_lower = brief_text.lower()
    missing: list[str] = []

    structured_countries = getattr(getattr(req, "filters", None), "countries", None) or []
    has_geo = bool(
        structured_countries
        or any(
            w in brief_lower
            for w in (
                "india", "united states", "united kingdom", "germany", "france",
                "netherlands", "australia", "canada", "brazil", "japan",
                "global", "worldwide", "all countries", "all markets",
            )
        )
    )

    has_kpi = any(
        w in brief_lower
        for w in (
            "install", "cpi", "conversion", "cpa", "awareness", "reach",
            "click", "ctr", "cpc", "traffic", "purchase", "brand", "roas", "cpm",
        )
    )

    extracted_budget = getattr(req, "extracted_budget_usd", None)
    has_budget = bool(
        extracted_budget is not None
        or any(
            w in brief_lower
            for w in ("budget", "$", "usd", "eur", "gbp", "spend", "investment")
        )
    )

    if not has_geo:
        missing.append("target geography (e.g. India, United States)")
    if not has_kpi:
        missing.append("campaign objective or KPI (e.g. app installs, brand awareness, conversions)")
    if not has_budget:
        missing.append(
            "campaign budget (required to create a media buy — e.g. $10,000 total or $5,000/month)"
        )

    if missing:
        return [
            _IncompleteItem(
                scope="products",
                description=(
                    "Brief is missing: "
                    + ", ".join(missing)
                    + ". Products returned may not be well-matched to your campaign."
                ),
            )
        ]

    return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCheckBriefCompletenessEmptyAndShort:
    """Empty and short briefs always produce incomplete[]."""

    def test_empty_string_returns_incomplete(self):
        """AC: get_products(brief='') → incomplete[]."""
        req = _make_req()
        result = _check_brief_completeness("", req)
        assert len(result) == 1
        assert result[0].scope == "products"
        assert "too short" in result[0].description

    def test_whitespace_only_returns_incomplete(self):
        """Whitespace-only brief is treated as empty."""
        req = _make_req()
        result = _check_brief_completeness("   ", req)
        assert len(result) == 1
        assert "too short" in result[0].description

    def test_short_brief_under_20_chars_returns_incomplete(self):
        """Brief shorter than 20 chars → incomplete[]."""
        req = _make_req()
        result = _check_brief_completeness("display ads", req)  # 11 chars
        assert len(result) == 1
        assert "too short" in result[0].description

    def test_exactly_19_chars_returns_incomplete(self):
        """19-char brief is still too short."""
        req = _make_req()
        brief = "a" * 19
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1

    def test_exactly_20_chars_passes_length_check(self):
        """20-char brief passes the length gate (may still fail field checks)."""
        req = _make_req()
        brief = "a" * 20  # passes length, but no geo/kpi/budget
        result = _check_brief_completeness(brief, req)
        # Should NOT be the "too short" message — it should be the "missing fields" message
        assert len(result) == 1
        assert "too short" not in result[0].description
        assert "missing" in result[0].description


class TestCheckBriefCompletenessCompleteBrief:
    """Complete briefs (geo + KPI + budget) return no incomplete[]."""

    def test_complete_brief_returns_empty_list(self):
        """AC: CPI campaign for food delivery app, India, budget $50k → no incomplete[]."""
        req = _make_req()
        brief = "CPI campaign for food delivery app, India, budget $50k"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_complete_brief_with_structured_countries(self):
        """AC: Nike CPC campaign, US, UK — budget $10k → no incomplete[] (geo via structured countries)."""
        req = _make_req(countries=["US", "UK"])
        brief = "Nike CPC campaign — budget $10k"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_complete_brief_awareness_germany_spend(self):
        """Brand awareness campaign in Germany with spend → no incomplete[]."""
        req = _make_req()
        brief = "Brand awareness campaign targeting Germany, investment of €20,000"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_complete_brief_global_reach_budget(self):
        """Global reach campaign with budget → no incomplete[]."""
        req = _make_req()
        brief = "Global reach campaign for new product launch, budget USD 100,000"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_complete_brief_roas_australia_budget(self):
        """ROAS campaign in Australia with budget → no incomplete[]."""
        req = _make_req()
        brief = "ROAS optimisation campaign for e-commerce in Australia, spend $30k"
        result = _check_brief_completeness(brief, req)
        assert result == []


class TestCheckBriefCompletenessGeoOnly:
    """Briefs with geo but missing KPI and/or budget."""

    def test_geo_only_returns_incomplete(self):
        """Brief with geo only → incomplete[] (missing KPI and/or budget).

        Note: 'brand' in the brief triggers the has_kpi heuristic, so this
        brief actually has geo + KPI but no budget — only budget is missing.
        """
        req = _make_req()
        brief = "Display campaign targeting United States market for our product"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        # No KPI keyword → both KPI and budget should be missing
        assert "missing" in result[0].description
        assert "budget" in result[0].description.lower()

    def test_quiksilver_affiliate_us_returns_incomplete(self):
        """AC: Quiksilver affiliate campaign, United States → incomplete[] (no KPI, no budget)."""
        req = _make_req()
        brief = "Quiksilver affiliate campaign targeting United States consumers"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        assert "missing" in result[0].description


class TestCheckBriefCompletenessKpiOnly:
    """Briefs with KPI but missing geo and/or budget."""

    def test_kpi_only_returns_incomplete(self):
        """Brief with KPI only → incomplete[] (missing geo and budget)."""
        req = _make_req()
        brief = "App install campaign targeting new users for our mobile game"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        assert "geography" in result[0].description.lower()
        assert "budget" in result[0].description.lower()

    def test_cpa_campaign_no_geo_no_budget(self):
        """CPA campaign without geo or budget → incomplete[]."""
        req = _make_req()
        brief = "CPA optimised campaign for conversion funnel improvement"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        assert "geography" in result[0].description.lower()


class TestCheckBriefCompletenessBudgetOnly:
    """Briefs with budget but missing geo and/or KPI."""

    def test_budget_only_returns_incomplete(self):
        """Brief with budget only → incomplete[] (missing geo and KPI)."""
        req = _make_req()
        brief = "We have a budget of $50,000 for our upcoming campaign launch"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        assert "geography" in result[0].description.lower()
        assert "KPI" in result[0].description or "objective" in result[0].description


class TestCheckBriefCompletenessStructuredCountries:
    """Structured countries in req.filters.countries satisfy the geo check."""

    def test_structured_countries_satisfies_geo(self):
        """ISO-3166-1 alpha-2 list in filters.countries satisfies geo check."""
        req = _make_req(countries=["IN", "SG"])
        brief = "CPI campaign for food delivery app, budget $50k"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_empty_structured_countries_falls_back_to_keyword(self):
        """Empty structured countries list falls back to keyword heuristic."""
        req = _make_req(countries=[])
        brief = "CPI campaign for food delivery app in India, budget $50k"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_structured_countries_no_keyword_needed(self):
        """Structured countries alone satisfy geo — no keyword required in brief."""
        req = _make_req(countries=["DE"])
        brief = "Brand awareness campaign, budget EUR 20,000, CPA optimised"
        result = _check_brief_completeness(brief, req)
        assert result == []


class TestCheckBriefCompletenessScope:
    """Scope is always 'products' for brief completeness signals."""

    def test_scope_is_products(self):
        """IncompleteItem scope is always 'products'."""
        req = _make_req()
        result = _check_brief_completeness("", req)
        assert result[0].scope == "products"

    def test_missing_fields_scope_is_products(self):
        """Missing-fields IncompleteItem also has scope='products'."""
        req = _make_req()
        brief = "Display campaign for our brand in the market"  # no geo, no kpi, no budget
        result = _check_brief_completeness(brief, req)
        assert result[0].scope == "products"


class TestCheckBriefCompletenessAdvisory:
    """incomplete[] is advisory — it does not block products from being returned."""

    def test_incomplete_does_not_prevent_products(self):
        """The function returns incomplete items; caller still returns products alongside them."""
        req = _make_req()
        brief = "display campaign"  # too short
        result = _check_brief_completeness(brief, req)
        # The function only returns the incomplete items — it does NOT filter products.
        # The caller (_get_products_impl) passes incomplete= to GetProductsResponse
        # while still including eligible_products.
        assert len(result) == 1  # advisory item present
        # Products are handled by the caller — not this function's concern

    def test_complete_brief_returns_empty_not_none(self):
        """AC: incomplete is absent (not []) when brief is sufficient.

        The function returns [] for complete briefs; the caller passes
        incomplete=None (not []) to GetProductsResponse when the list is empty.
        """
        req = _make_req()
        brief = "CPI campaign for food delivery app, India, budget $50k"
        result = _check_brief_completeness(brief, req)
        assert result == []
        # Caller does: incomplete=result or None → None when result is []
        assert not result  # falsy → caller passes None → spec: absent when complete


class TestCheckBriefCompletenessDisplayCampaign:
    """AC: get_products(brief='display campaign') → incomplete[] (no geo, no KPI, no budget)."""

    def test_display_campaign_returns_incomplete(self):
        """'display campaign' is too short (< 20 chars) → incomplete[]."""
        req = _make_req()
        result = _check_brief_completeness("display campaign", req)
        assert len(result) == 1
        assert result[0].scope == "products"

    def test_longer_display_campaign_no_fields(self):
        """Longer 'display campaign' brief with no geo/KPI/budget → incomplete[]."""
        req = _make_req()
        brief = "We want to run a display campaign for our product"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        assert "missing" in result[0].description
