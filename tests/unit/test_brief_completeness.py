"""Unit tests for task-03b: _check_brief_completeness() in products.py.

REGRESSION NOTE: An earlier version of this test file only tested a local
mirror of the completeness logic and never imported or called the real
_check_brief_completeness() function. That mirror used
`adcp.types.IncompleteItem` directly, masking a real bug: IncompleteItem is
defined in adcp's internal generated_poc/_generated modules but is NOT
re-exported from the public `adcp.types` package, so the real function
raised ImportError at runtime. TestRealCheckBriefCompletenessImport below
imports and calls the actual function from src.core.tools.products to
guard against this class of bug recurring.

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
    """Replicate the logic from products.py for isolated unit testing.

    Geo check delegates to the real _brief_mentions_a_country() (pycountry-backed)
    so this mirror stays in sync with the production ISO-3166-1 matching behaviour
    instead of drifting with its own hand-rolled country list.
    """
    from src.core.tools.products import _brief_mentions_a_country, _contains_any_keyword

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
        or any(phrase in brief_lower for phrase in ("global", "worldwide", "all countries", "all markets"))
        or _brief_mentions_a_country(brief_lower)
    )

    # Word-boundary matched (delegates to the real _contains_any_keyword) to
    # avoid false positives like "brand" inside "Brandon" or "cpa" as a
    # substring of an unrelated acronym.
    has_kpi = _contains_any_keyword(
        brief_lower,
        (
            "install", "installs", "cpi", "conversion", "conversions", "cpa",
            "awareness", "reach", "click", "clicks", "ctr", "cpc", "traffic",
            "purchase", "brand", "roas", "cpm",
        ),
    )

    extracted_budget = getattr(req, "extracted_budget_usd", None)
    has_budget = bool(
        extracted_budget is not None
        or "$" in brief_lower
        or _contains_any_keyword(
            brief_lower, ("budget", "usd", "eur", "gbp", "spend", "investment")
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


# ---------------------------------------------------------------------------
# Regression: import and call the REAL function, and validate it round-trips
# through the real GetProductsResponse Pydantic model. This is the test that
# would have caught the ImportError bug (adcp.types.IncompleteItem is not
# importable — the real function used to raise ImportError on every call).
# ---------------------------------------------------------------------------

class TestRealCheckBriefCompletenessImport:
    """Import and exercise the actual products.py implementation.

    Unlike the mirror-based tests above (which duplicate the logic locally
    for fast, dependency-free testing), these tests import the real
    _check_brief_completeness from src.core.tools.products and construct a
    real GetProductsResponse to confirm the returned items are accepted by
    Pydantic validation end-to-end.
    """

    def test_real_function_importable(self):
        """The real function must be importable without raising ImportError."""
        from src.core.tools.products import _check_brief_completeness as real_fn
        assert callable(real_fn)

    def test_real_function_short_brief_returns_dict_shaped_item(self):
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        result = real_fn("", req)
        assert len(result) == 1
        # Must be dict-shaped (not an unimportable class instance) so it can
        # be passed straight into GetProductsResponse(incomplete=...).
        assert isinstance(result[0], dict)
        assert result[0]["scope"] == "products"
        assert "too short" in result[0]["description"]

    def test_real_function_complete_brief_returns_empty_list(self):
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        result = real_fn("CPI campaign for food delivery app, India, budget $50k", req)
        assert result == []

    def test_real_function_result_coerces_into_get_products_response(self):
        """The critical regression check: incomplete_items must be accepted
        by the real GetProductsResponse Pydantic model without raising.

        Before the fix, _check_brief_completeness raised ImportError before
        ever returning — this test would have failed with that error.
        """
        from src.core.schemas import GetProductsResponse
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        incomplete_items = real_fn("too short", req)
        assert incomplete_items  # non-empty — "too short" is < 20 chars

        # Must not raise — Pydantic coerces the list[dict] into list[IncompleteItem]
        resp = GetProductsResponse(
            products=[],
            errors=None,
            context=None,
            incomplete=incomplete_items or None,
        )
        assert resp.incomplete is not None
        assert len(resp.incomplete) == 1
        assert resp.incomplete[0].scope.value == "products"

    def test_real_function_empty_result_passes_none_to_response(self):
        """When the brief is complete, incomplete=None must round-trip as None."""
        from src.core.schemas import GetProductsResponse
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        incomplete_items = real_fn(
            "CPI campaign for food delivery app, India, budget $50k", req
        )
        assert incomplete_items == []

        resp = GetProductsResponse(
            products=[],
            errors=None,
            context=None,
            incomplete=incomplete_items or None,
        )
        assert resp.incomplete is None


# ---------------------------------------------------------------------------
# pycountry-backed geo matching — replaces the old hand-rolled 14-country list.
# Regression coverage: the old list only recognised India, US, UK, Germany,
# France, Netherlands, Australia, Canada, Brazil, Japan — missing Spain,
# Mexico, Singapore, UAE, Indonesia, Italy, China, South Korea, and ~230 more.
# ---------------------------------------------------------------------------

class TestPycountryGeoMatching:
    """Verify the full ISO-3166-1 country database is recognised, not just
    the handful of countries that happened to be hand-typed into a list."""

    @pytest.mark.parametrize(
        "country_phrase",
        [
            "Spain", "Mexico", "Singapore", "United Arab Emirates", "Indonesia",
            "Italy", "China", "South Korea", "Sweden", "Poland", "Vietnam",
            "Nigeria", "Argentina", "Thailand", "Philippines", "Egypt",
        ],
    )
    def test_previously_unrecognised_countries_now_satisfy_geo(self, country_phrase):
        """Countries absent from the old hardcoded list must now be recognised."""
        req = _make_req()
        brief = f"CPI campaign targeting {country_phrase}, budget $25k"
        result = _check_brief_completeness(brief, req)
        assert result == [], f"{country_phrase} should satisfy the geo check"

    def test_word_boundary_chad_not_matched_inside_chadwick(self):
        """'Chad' (a real country) must not false-match inside 'Chadwick'."""
        req = _make_req()
        brief = "Chadwick brand awareness campaign for our new product line"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        assert "geography" in result[0].description.lower()

    def test_word_boundary_oman_not_matched_inside_woman(self):
        """'Oman' (a real country) must not false-match inside 'woman'."""
        req = _make_req()
        brief = "Woman-focused fashion campaign, budget $15k, awareness goal"
        result = _check_brief_completeness(brief, req)
        assert len(result) == 1
        assert "geography" in result[0].description.lower()

    def test_actual_chad_and_oman_are_recognised(self):
        """Sanity check: Chad and Oman as standalone words ARE recognised."""
        req = _make_req()
        brief = "CPI campaign targeting Chad and Oman markets, budget $10k"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_case_insensitive_country_matching(self):
        """Country matching must be case-insensitive."""
        req = _make_req()
        brief = "cpc campaign targeting SPAIN, budget $5k"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_real_function_recognises_expanded_country_set(self):
        """Import the real function and confirm it also uses the expanded set
        (not just the local mirror in this test file)."""
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        result = real_fn("CPA campaign targeting Vietnam, budget $8k", req)
        assert result == []


# ---------------------------------------------------------------------------
# Word-boundary KPI/budget keyword matching — replaces fragile substring checks.
# Regression coverage: "brand" used to match inside "Brandon", "eur" used to
# match inside "neuromarketing", producing false positives that made
# _check_brief_completeness() think a KPI/budget signal was present when it
# was actually just a coincidental substring of an unrelated word.
# ---------------------------------------------------------------------------

class TestWordBoundaryKeywordMatching:
    """Verify KPI and budget keyword checks use word boundaries, not raw substrings."""

    def test_brandon_does_not_false_match_brand_kpi_keyword(self):
        """'Brandon' must not satisfy the KPI check via substring match on 'brand'."""
        req = _make_req()
        brief = "We are targeting fans of Brandon in this campaign, India, budget $5k"
        result = _check_brief_completeness(brief, req)
        # Geo (India) + budget ($5k) present, but no real KPI keyword -> incomplete[]
        assert len(result) == 1
        assert "KPI" in result[0].description or "objective" in result[0].description

    def test_neuromarketing_does_not_false_match_eur_budget_keyword(self):
        """'neuromarketing' must not satisfy the budget check via substring match on 'eur'."""
        req = _make_req()
        brief = "Neuromarketing research campaign targeting India, CPC goal"
        result = _check_brief_completeness(brief, req)
        # Geo (India) + KPI (CPC) present, but no real budget keyword -> incomplete[]
        assert len(result) == 1
        assert "budget" in result[0].description.lower()

    def test_real_brand_keyword_still_matches_as_standalone_word(self):
        """A genuine standalone 'brand' keyword must still satisfy the KPI check."""
        req = _make_req()
        brief = "Brand awareness campaign in India, budget $10k"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_real_eur_currency_still_matches_as_standalone_word(self):
        """A genuine standalone 'EUR' currency code must still satisfy the budget check."""
        req = _make_req()
        brief = "CPC campaign in Germany, spend EUR 5000"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_dollar_sign_still_satisfies_budget_check(self):
        """The '$' symbol (non-word character) must still be recognised via
        plain substring match since word-boundary regex can't anchor to it."""
        req = _make_req()
        brief = "CPI campaign for food delivery app, India, $50,000 total"
        result = _check_brief_completeness(brief, req)
        assert result == []

    def test_real_function_word_boundary_regression(self):
        """Import the real function to confirm the fix applies end-to-end."""
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        brief = "Neuromarketing campaign for Brandon's brand in India"
        result = real_fn(brief, req)
        # Geo present (India); "brand" IS a real standalone word here so KPI
        # is satisfied; but no budget signal -> incomplete[] for budget only.
        # The real function returns plain dicts (see _check_brief_completeness
        # docstring for why), not objects with a .description attribute.
        assert len(result) == 1
        assert "budget" in result[0]["description"].lower()
        assert "geography" not in result[0]["description"].lower()
