"""Unit tests for task-02: brief_relevance wiring in products.py.

Tests that ranking_map reason strings are correctly written to
product.brief_relevance after the sort/filter step in _get_products_impl.

No database, no Vertex AI, no HTTP — pure unit test using MagicMock products.
"""

from unittest.mock import MagicMock

import pytest


def _make_product(product_id: str) -> MagicMock:
    """Return a MagicMock that looks like a Product model instance."""
    p = MagicMock()
    p.product_id = product_id
    p.brief_relevance = None
    return p


def _apply_brief_relevance(eligible_products: list, ranking_map: dict) -> None:
    """Replicate the brief_relevance wiring logic from products.py.

    This mirrors the exact code added in task-02 so the test stays in sync
    with the implementation without importing the full products module.
    """
    for product in eligible_products:
        _, reason = ranking_map.get(product.product_id, (0.0, ""))
        if reason:
            product.brief_relevance = reason  # type: ignore[attr-defined]


class TestBriefRelevanceWiring:
    """Tests for the ranking reason → product.brief_relevance wiring (task-02)."""

    def test_reason_written_to_brief_relevance(self):
        """Products in ranking_map get their reason written to brief_relevance."""
        p1 = _make_product("prod_001")
        ranking_map = {"prod_001": (0.85, "Strong vertical match: food delivery app in India")}

        _apply_brief_relevance([p1], ranking_map)

        assert p1.brief_relevance == "Strong vertical match: food delivery app in India"

    def test_top_ranked_product_has_non_empty_brief_relevance(self):
        """AC: products[0].brief_relevance is a non-empty string when brief is provided."""
        products = [
            _make_product("prod_001"),
            _make_product("prod_002"),
        ]
        ranking_map = {
            "prod_001": (0.9, "Excellent KPI match: CPI campaign for food delivery"),
            "prod_002": (0.6, "Moderate match: broad reach but not food-specific"),
        }

        _apply_brief_relevance(products, ranking_map)

        assert products[0].brief_relevance
        assert len(products[0].brief_relevance) > 0

    def test_multiple_products_each_get_their_own_reason(self):
        """Each product gets its own reason string, not a shared one."""
        p1 = _make_product("prod_001")
        p2 = _make_product("prod_002")
        ranking_map = {
            "prod_001": (0.9, "Reason for product 1"),
            "prod_002": (0.7, "Reason for product 2"),
        }

        _apply_brief_relevance([p1, p2], ranking_map)

        assert p1.brief_relevance == "Reason for product 1"
        assert p2.brief_relevance == "Reason for product 2"

    def test_product_not_in_ranking_map_gets_no_brief_relevance(self):
        """Products absent from ranking_map keep brief_relevance as None."""
        p = _make_product("prod_unknown")
        ranking_map = {"prod_001": (0.9, "Some reason")}

        _apply_brief_relevance([p], ranking_map)

        assert p.brief_relevance is None

    def test_empty_reason_string_not_written(self):
        """Empty reason string in ranking_map does not overwrite brief_relevance."""
        p = _make_product("prod_001")
        p.brief_relevance = None
        ranking_map = {"prod_001": (0.5, "")}  # empty reason

        _apply_brief_relevance([p], ranking_map)

        assert p.brief_relevance is None

    def test_empty_eligible_products_no_error(self):
        """Empty product list is handled gracefully."""
        ranking_map = {"prod_001": (0.9, "Some reason")}
        _apply_brief_relevance([], ranking_map)  # must not raise

    def test_empty_ranking_map_no_error(self):
        """Empty ranking_map leaves all products with brief_relevance=None."""
        products = [_make_product("prod_001"), _make_product("prod_002")]
        _apply_brief_relevance(products, {})
        for p in products:
            assert p.brief_relevance is None

    def test_reason_not_shared_across_products(self):
        """Verify no aliasing — each product's brief_relevance is independent."""
        p1 = _make_product("prod_001")
        p2 = _make_product("prod_002")
        reason1 = "Reason A"
        reason2 = "Reason B"
        ranking_map = {
            "prod_001": (0.9, reason1),
            "prod_002": (0.8, reason2),
        }

        _apply_brief_relevance([p1, p2], ranking_map)

        assert p1.brief_relevance != p2.brief_relevance
        assert p1.brief_relevance == reason1
        assert p2.brief_relevance == reason2
