"""Unit tests for task-03b: _check_brief_completeness() in products.py.

The buyer agent's LLM extraction (brief_extraction_agent) runs before get_products
is called and serialises structured fields into the brief string via
build_brief_string() — e.g. "Countries: IN, US. Budget: USD 50,000. KPI: CPI ≤ $2".

_check_brief_completeness() detects completeness by looking for those structured
labels ("Countries:", "KPI:", "Objective:", "Budget:"), not by scanning free text
for country names or KPI keywords. This is simpler and more reliable.

Acceptance criteria from task-03b-salesagent-incomplete-handling.md:
- get_products(brief="") → incomplete: [{scope: "products", description: "..."}]
- get_products(brief="CPI campaign for food delivery app, India, budget $50k") → no incomplete[]
  (in practice the buyer agent enriches this to include "Countries:", "KPI:", "Budget:" labels)
- get_products(brief="display campaign") → incomplete[] (no structured labels)
- get_products(brief="Nike CPC campaign, US, UK — budget $10k") → no incomplete[]
  (buyer agent enriches with "Countries: US, GB. KPI: CPC. Budget: USD 10,000")
- get_products(brief="Quiksilver affiliate campaign, United States") → incomplete[]
  (no KPI label, no Budget label)
- incomplete is absent (not []) when brief is sufficient
- Products still returned alongside incomplete[] — advisory, not a hard block
- Unit tests: empty brief, short brief, geo only, KPI only, budget only, complete brief

No database, no Vertex AI, no HTTP — pure unit test.
"""

from unittest.mock import MagicMock

import pytest

from src.core.tools.products import _check_brief_completeness


def _make_req() -> MagicMock:
    """Return a MagicMock that looks like a GetProductsRequest."""
    req = MagicMock()
    req.filters = MagicMock()
    req.filters.countries = []
    return req


# ---------------------------------------------------------------------------
# Short / empty brief — always incomplete regardless of content
# ---------------------------------------------------------------------------

class TestShortBrief:
    """Briefs under 20 chars are always incomplete."""

    def test_empty_string_returns_incomplete(self):
        """AC: get_products(brief='') → incomplete[]."""
        result = _check_brief_completeness("", _make_req())
        assert len(result) == 1
        assert result[0]["scope"] == "products"
        assert "too short" in result[0]["description"].lower()

    def test_whitespace_only_returns_incomplete(self):
        result = _check_brief_completeness("   ", _make_req())
        assert len(result) == 1

    def test_short_brief_under_20_chars_returns_incomplete(self):
        result = _check_brief_completeness("short", _make_req())
        assert len(result) == 1

    def test_exactly_19_chars_returns_incomplete(self):
        result = _check_brief_completeness("a" * 19, _make_req())
        assert len(result) == 1

    def test_exactly_20_chars_passes_length_check(self):
        """20 chars passes the length gate — but will still be incomplete (no labels)."""
        result = _check_brief_completeness("a" * 20, _make_req())
        # Length gate passed, but no structured labels → still incomplete
        assert len(result) == 1
        assert "missing" in result[0]["description"].lower()


# ---------------------------------------------------------------------------
# Complete briefs — LLM-enriched strings with all three structured labels
# ---------------------------------------------------------------------------

class TestCompleteBrief:
    """Briefs with all three structured labels → no incomplete[]."""

    def test_complete_enriched_brief_returns_empty_list(self):
        """AC: LLM-enriched brief with Countries, KPI, Budget → no incomplete[]."""
        brief = (
            "CPI campaign for food delivery app. "
            "Objective: app_installs. KPI: CPI ≤ $2. "
            "Countries: IN. Budget: USD 50,000"
        )
        result = _check_brief_completeness(brief, _make_req())
        assert result == []

    def test_nike_cpc_enriched_brief_returns_empty_list(self):
        """AC: Nike CPC campaign enriched by buyer agent → no incomplete[]."""
        brief = (
            "Nike CPC campaign. "
            "KPI: CPC. "
            "Countries: US, GB. "
            "Budget: USD 10,000"
        )
        result = _check_brief_completeness(brief, _make_req())
        assert result == []

    def test_awareness_germany_enriched_brief(self):
        """Brand awareness campaign enriched with all labels → no incomplete[]."""
        brief = (
            "Brand awareness campaign. "
            "Objective: brand_awareness. KPI: reach > 1M. "
            "Countries: DE. Budget: EUR 20,000"
        )
        result = _check_brief_completeness(brief, _make_req())
        assert result == []

    def test_global_reach_enriched_brief(self):
        """Global campaign — 'global' in brief satisfies geo check."""
        brief = (
            "Global reach campaign for new product launch. "
            "KPI: brand awareness. Budget: USD 100,000"
        )
        result = _check_brief_completeness(brief, _make_req())
        assert result == []

    def test_worldwide_satisfies_geo(self):
        """'worldwide' in brief satisfies geo check."""
        brief = "Worldwide CPC campaign. KPI: CPC. Budget: USD 5,000"
        result = _check_brief_completeness(brief, _make_req())
        assert result == []

    def test_dollar_sign_satisfies_budget(self):
        """'$' in brief satisfies budget check."""
        brief = "Countries: IN. KPI: CPI. $50,000 total budget"
        result = _check_brief_completeness(brief, _make_req())
        assert result == []

    def test_objective_label_satisfies_kpi(self):
        """'Objective:' label satisfies KPI check."""
        brief = "Countries: US. Objective: app_installs. Budget: USD 10,000"
        result = _check_brief_completeness(brief, _make_req())
        assert result == []


# ---------------------------------------------------------------------------
# Missing geo — no "Countries:", "global", or "worldwide"
# ---------------------------------------------------------------------------

class TestMissingGeo:
    """Briefs with KPI and budget but no geo → incomplete[]."""

    def test_no_geo_label_returns_incomplete(self):
        """AC: display campaign with no geo → incomplete[]."""
        brief = "Display campaign. KPI: brand awareness. Budget: USD 10,000"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        assert "geography" in result[0]["description"].lower()

    def test_quiksilver_no_kpi_no_budget(self):
        """AC: Quiksilver affiliate campaign — geo present but no KPI, no budget."""
        # Raw brief without LLM enrichment — no structured labels
        brief = "Quiksilver affiliate campaign, United States"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        # Missing KPI and budget (geo also missing — no "Countries:" label)
        assert "missing" in result[0]["description"].lower()

    def test_country_name_in_raw_text_does_not_satisfy_geo(self):
        """Without LLM enrichment, a country name in raw text is NOT enough.

        The check looks for 'Countries:' label, not country names in free text.
        This is intentional — the buyer agent's LLM extraction is the authoritative
        geo source. If it didn't run, the brief is genuinely thin.
        """
        brief = "CPI campaign targeting India. KPI: CPI. Budget: USD 10,000"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        assert "geography" in result[0]["description"].lower()


# ---------------------------------------------------------------------------
# Missing KPI — no "KPI:" or "Objective:" label
# ---------------------------------------------------------------------------

class TestMissingKpi:
    """Briefs with geo and budget but no KPI label → incomplete[]."""

    def test_no_kpi_label_returns_incomplete(self):
        brief = "Countries: IN. Budget: USD 10,000. Display campaign"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        assert "kpi" in result[0]["description"].lower() or "objective" in result[0]["description"].lower()

    def test_kpi_keyword_in_raw_text_does_not_satisfy_kpi(self):
        """'brand' or 'cpi' in raw text without 'KPI:' label is not enough."""
        brief = "Countries: IN. Budget: USD 10,000. Brand campaign for awareness"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        assert "kpi" in result[0]["description"].lower() or "objective" in result[0]["description"].lower()


# ---------------------------------------------------------------------------
# Missing budget — no "Budget:" label or "$"
# ---------------------------------------------------------------------------

class TestMissingBudget:
    """Briefs with geo and KPI but no budget → incomplete[]."""

    def test_no_budget_label_returns_incomplete(self):
        brief = "Countries: IN. KPI: CPI ≤ $2. CPI campaign for food delivery"
        # Note: "$2" in the KPI line satisfies budget check via "$" substring
        # Use a brief without any "$" to test the budget-missing path
        brief = "Countries: IN. KPI: CPI. CPI campaign for food delivery"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        assert "budget" in result[0]["description"].lower()

    def test_budget_keyword_in_raw_text_does_not_satisfy_budget(self):
        """'spend' or 'investment' in raw text without 'Budget:' label is not enough."""
        brief = "Countries: IN. KPI: CPI. High spend campaign"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        assert "budget" in result[0]["description"].lower()


# ---------------------------------------------------------------------------
# Scope and structure of incomplete[] items
# ---------------------------------------------------------------------------

class TestIncompleteItemStructure:
    """Verify the shape of returned incomplete[] items."""

    def test_scope_is_always_products(self):
        result = _check_brief_completeness("display campaign", _make_req())
        assert result[0]["scope"] == "products"

    def test_description_mentions_missing_fields(self):
        brief = "display campaign with no labels at all and enough length here"
        result = _check_brief_completeness(brief, _make_req())
        assert "missing" in result[0]["description"].lower()
        assert "geography" in result[0]["description"].lower()
        assert "kpi" in result[0]["description"].lower() or "objective" in result[0]["description"].lower()
        assert "budget" in result[0]["description"].lower()

    def test_only_missing_fields_are_listed(self):
        """When only geo is missing, only geo appears in description."""
        brief = "KPI: CPI. Budget: USD 10,000. CPI campaign"
        result = _check_brief_completeness(brief, _make_req())
        assert len(result) == 1
        assert "geography" in result[0]["description"].lower()
        assert "budget" not in result[0]["description"].lower()
        assert "kpi" not in result[0]["description"].lower()

    def test_complete_brief_returns_empty_not_none(self):
        """AC: incomplete is absent (not []) when brief is sufficient.

        The caller passes incomplete=items or None to GetProductsResponse.
        An empty list from this function → None → field absent from response.
        """
        brief = "Countries: IN. KPI: CPI. Budget: USD 50,000"
        result = _check_brief_completeness(brief, _make_req())
        assert result == []
        assert isinstance(result, list)

    def test_incomplete_does_not_prevent_products(self):
        """AC: Products are still returned alongside incomplete[] — advisory only.

        This function only returns the incomplete[] items; it does not raise
        or block. The caller (_get_products_impl) always builds GetProductsResponse
        with both products and incomplete.
        """
        # Just verify the function returns a list (not raises)
        result = _check_brief_completeness("display campaign", _make_req())
        assert isinstance(result, list)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Real function import — guard against import regressions
# ---------------------------------------------------------------------------

class TestRealFunctionImport:
    """Import and call the actual function to catch import/runtime errors."""

    def test_real_function_importable(self):
        from src.core.tools.products import _check_brief_completeness as real_fn
        assert callable(real_fn)

    def test_real_function_short_brief_returns_dict_shaped_item(self):
        from src.core.tools.products import _check_brief_completeness as real_fn
        req = _make_req()
        result = real_fn("short", req)
        assert len(result) == 1
        assert result[0]["scope"] == "products"
        assert isinstance(result[0]["description"], str)

    def test_real_function_complete_brief_returns_empty_list(self):
        from src.core.tools.products import _check_brief_completeness as real_fn
        req = _make_req()
        brief = "Countries: IN. KPI: CPI ≤ $2. Budget: USD 50,000. CPI campaign"
        result = real_fn(brief, req)
        assert result == []

    def test_real_function_result_coerces_into_get_products_response(self):
        """Verify plain dicts coerce into GetProductsResponse.incomplete via Pydantic."""
        from adcp import GetProductsResponse
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        incomplete_items = real_fn("display campaign", req)
        assert len(incomplete_items) == 1

        resp = GetProductsResponse(
            products=[],
            errors=None,
            context=None,
            incomplete=incomplete_items,
        )
        assert resp.incomplete is not None
        assert len(resp.incomplete) == 1
        # scope is a Scope enum after Pydantic coercion — compare .value
        assert resp.incomplete[0].scope.value == "products"

    def test_real_function_empty_result_passes_none_to_response(self):
        """When brief is complete, incomplete=None → field absent from response."""
        from adcp import GetProductsResponse
        from src.core.tools.products import _check_brief_completeness as real_fn

        req = _make_req()
        brief = "Countries: IN. KPI: CPI. Budget: USD 50,000"
        incomplete_items = real_fn(brief, req)
        assert incomplete_items == []

        resp = GetProductsResponse(
            products=[],
            errors=None,
            context=None,
            incomplete=incomplete_items or None,
        )
        assert resp.incomplete is None
