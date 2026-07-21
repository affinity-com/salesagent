"""Unit tests for Task 11 — Starting Bid Derivation (D17).

Covers all acceptance criteria from siteplug-task11.md:

- _derive_starting_bid() returns correct tier-based bid when no explicit bid_price
- Explicit package.bid_price always overrides derived bid
- Tier 1 geo (US/CA/GB/DE/FR/IT/ES) → SD $0.10, SSS $0.05, SDC $0.10
- Tier 2 geo (AU/other EU) → $0.05 for all product types
- Tier 3 geo (IN/BR/MX) → $0.01 for all product types
- Multi-country targeting uses the highest tier (lowest number) present
- No geo targeting defaults to Tier 2 bid
- Low-volume zones (< low_threshold) apply 1.5× multiplier
- High-volume zones (> high_threshold) apply 0.7× multiplier
- No volume data → 1.0× multiplier (no adjustment)
- bid_volume_low_threshold and bid_volume_high_threshold are configurable
- _get_zone_volume returns None gracefully when no zone_stats column
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.adapters.siteplug.managers.campaign import (
    BASE_BIDS,
    GEO_TIER_1,
    GEO_TIER_2,
    SiteplugCampaignManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager() -> SiteplugCampaignManager:
    """Return a SiteplugCampaignManager with a mock client."""
    client = MagicMock()
    return SiteplugCampaignManager(client=client)


def _make_request(countries: list[str] | None = None, product_id: str = "prod-1") -> SimpleNamespace:
    """Build a minimal AdCP create_media_buy request stub."""
    if countries is not None:
        geo = SimpleNamespace(countries=countries)
        targeting = SimpleNamespace(geo=geo)
    else:
        targeting = SimpleNamespace(geo=None)
    return SimpleNamespace(targeting=targeting, product_id=product_id)


def _make_package(bid_price: float | None = None) -> SimpleNamespace:
    """Build a minimal AdCP package stub."""
    return SimpleNamespace(bid_price=bid_price)


def _make_product_config(
    low_threshold: int | None = None,
    high_threshold: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        bid_volume_low_threshold=low_threshold,
        bid_volume_high_threshold=high_threshold,
    )


# ---------------------------------------------------------------------------
# Module-level constant sanity checks
# ---------------------------------------------------------------------------

class TestConstants:
    def test_geo_tier_1_contains_expected_countries(self):
        assert {"US", "CA", "GB", "DE", "FR", "IT", "ES"} <= GEO_TIER_1

    def test_geo_tier_2_contains_expected_countries(self):
        assert {"AU", "AT", "BE", "CH", "DK", "FI", "NL", "NO", "PL", "PT", "SE"} <= GEO_TIER_2

    def test_tier_1_and_2_are_disjoint(self):
        assert GEO_TIER_1.isdisjoint(GEO_TIER_2)

    def test_base_bids_has_all_9_entries(self):
        assert len(BASE_BIDS) == 9

    def test_base_bids_tier1_sd(self):
        assert BASE_BIDS[(1, "SD")] == 0.10

    def test_base_bids_tier1_sss(self):
        assert BASE_BIDS[(1, "SSS")] == 0.05

    def test_base_bids_tier1_sdc(self):
        assert BASE_BIDS[(1, "SDC")] == 0.10

    def test_base_bids_tier2_all_types(self):
        assert BASE_BIDS[(2, "SD")] == 0.05
        assert BASE_BIDS[(2, "SSS")] == 0.05
        assert BASE_BIDS[(2, "SDC")] == 0.05

    def test_base_bids_tier3_all_types(self):
        assert BASE_BIDS[(3, "SD")] == 0.01
        assert BASE_BIDS[(3, "SSS")] == 0.01
        assert BASE_BIDS[(3, "SDC")] == 0.01


# ---------------------------------------------------------------------------
# _derive_starting_bid — explicit override
# ---------------------------------------------------------------------------

class TestExplicitBidOverride:
    def test_explicit_bid_price_overrides_derived_bid(self):
        """An explicit package.bid_price must always win, regardless of geo/volume."""
        mgr = _make_manager()
        request = _make_request(countries=["US"])
        package = _make_package(bid_price=0.99)

        result = mgr._derive_starting_bid(request, package, "SD")

        assert result == 0.99

    def test_explicit_bid_price_zero_is_respected(self):
        """bid_price=0.0 is a valid explicit override (not treated as falsy)."""
        mgr = _make_manager()
        request = _make_request(countries=["US"])
        package = _make_package(bid_price=0.0)

        result = mgr._derive_starting_bid(request, package, "SD")

        assert result == 0.0


# ---------------------------------------------------------------------------
# _derive_starting_bid — Tier 1 geo
# ---------------------------------------------------------------------------

class TestTier1Geo:
    @pytest.mark.parametrize("country", ["US", "CA", "GB", "DE", "FR", "IT", "ES"])
    def test_tier1_country_sd_returns_010(self, country):
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=[country]), _make_package(), "SD"
            )
        assert result == 0.10

    @pytest.mark.parametrize("country", ["US", "CA", "GB", "DE", "FR", "IT", "ES"])
    def test_tier1_country_sss_returns_005(self, country):
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=[country]), _make_package(), "SSS"
            )
        assert result == 0.05

    @pytest.mark.parametrize("country", ["US", "CA", "GB", "DE", "FR", "IT", "ES"])
    def test_tier1_country_sdc_returns_010(self, country):
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=[country]), _make_package(), "SDC"
            )
        assert result == 0.10


# ---------------------------------------------------------------------------
# _derive_starting_bid — Tier 2 geo
# ---------------------------------------------------------------------------

class TestTier2Geo:
    @pytest.mark.parametrize("country", ["AU", "AT", "BE", "CH", "DK", "FI", "NL", "NO", "PL", "PT", "SE"])
    def test_tier2_country_all_types_return_005(self, country):
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            for campaign_type in ("SD", "SSS", "SDC"):
                result = mgr._derive_starting_bid(
                    _make_request(countries=[country]), _make_package(), campaign_type
                )
                assert result == 0.05, f"Expected 0.05 for {country}/{campaign_type}, got {result}"


# ---------------------------------------------------------------------------
# _derive_starting_bid — Tier 3 geo
# ---------------------------------------------------------------------------

class TestTier3Geo:
    @pytest.mark.parametrize("country", ["IN", "BR", "MX", "ZA", "NG", "PH"])
    def test_tier3_country_all_types_return_001(self, country):
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            for campaign_type in ("SD", "SSS", "SDC"):
                result = mgr._derive_starting_bid(
                    _make_request(countries=[country]), _make_package(), campaign_type
                )
                assert result == 0.01, f"Expected 0.01 for {country}/{campaign_type}, got {result}"


# ---------------------------------------------------------------------------
# _derive_starting_bid — No geo → Tier 2 default
# ---------------------------------------------------------------------------

class TestNoGeoDefault:
    def test_no_countries_list_defaults_to_tier2(self):
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=None), _make_package(), "SD"
            )
        assert result == 0.05

    def test_empty_countries_list_defaults_to_tier2(self):
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=[]), _make_package(), "SSS"
            )
        assert result == 0.05

    def test_no_targeting_attribute_defaults_to_tier2(self):
        """Request with no targeting attribute at all → Tier 2."""
        mgr = _make_manager()
        request = SimpleNamespace(product_id="prod-1")  # no targeting attr
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(request, _make_package(), "SDC")
        assert result == 0.05


# ---------------------------------------------------------------------------
# _derive_starting_bid — Multi-country → highest tier wins
# ---------------------------------------------------------------------------

class TestMultiCountryTierSelection:
    def test_tier1_country_with_tier3_country_uses_tier1(self):
        """US (Tier 1) + IN (Tier 3) → Tier 1 wins."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US", "IN"]), _make_package(), "SD"
            )
        assert result == 0.10  # Tier 1 SD

    def test_tier2_country_with_tier3_country_uses_tier2(self):
        """AU (Tier 2) + BR (Tier 3) → Tier 2 wins."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=["AU", "BR"]), _make_package(), "SD"
            )
        assert result == 0.05  # Tier 2 SD

    def test_tier1_country_with_tier2_country_uses_tier1(self):
        """DE (Tier 1) + NL (Tier 2) → Tier 1 wins."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=["DE", "NL"]), _make_package(), "SSS"
            )
        assert result == 0.05  # Tier 1 SSS

    def test_country_codes_are_case_insensitive(self):
        """Lower-case country codes must be normalised to upper-case."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=["us", "de"]), _make_package(), "SD"
            )
        assert result == 0.10  # Tier 1 SD


# ---------------------------------------------------------------------------
# _derive_starting_bid — Volume multipliers
# ---------------------------------------------------------------------------

class TestVolumeMultipliers:
    def test_low_volume_applies_1_5x_multiplier(self):
        """Volume below low_threshold → ×1.5."""
        mgr = _make_manager()
        # Tier 1 SD base = 0.10; ×1.5 = 0.15
        with patch.object(mgr, "_get_zone_volume", return_value=5_000):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]),
                _make_package(),
                "SD",
                product_config=_make_product_config(low_threshold=10_000, high_threshold=1_000_000),
            )
        assert result == round(0.10 * 1.5, 4)  # 0.15

    def test_high_volume_applies_07x_multiplier(self):
        """Volume above high_threshold → ×0.7."""
        mgr = _make_manager()
        # Tier 1 SD base = 0.10; ×0.7 = 0.07
        with patch.object(mgr, "_get_zone_volume", return_value=2_000_000):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]),
                _make_package(),
                "SD",
                product_config=_make_product_config(low_threshold=10_000, high_threshold=1_000_000),
            )
        assert result == round(0.10 * 0.7, 4)  # 0.07

    def test_normal_volume_applies_1x_multiplier(self):
        """Volume between thresholds → ×1.0 (no adjustment)."""
        mgr = _make_manager()
        # Tier 1 SD base = 0.10; ×1.0 = 0.10
        with patch.object(mgr, "_get_zone_volume", return_value=500_000):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]),
                _make_package(),
                "SD",
                product_config=_make_product_config(low_threshold=10_000, high_threshold=1_000_000),
            )
        assert result == 0.10

    def test_no_volume_data_applies_1x_multiplier(self):
        """_get_zone_volume returns None → ×1.0 (no adjustment)."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]), _make_package(), "SD"
            )
        assert result == 0.10


# ---------------------------------------------------------------------------
# _derive_starting_bid — Configurable thresholds
# ---------------------------------------------------------------------------

class TestConfigurableThresholds:
    def test_custom_low_threshold_respected(self):
        """Custom low_threshold=50_000 — volume of 30_000 should trigger ×1.5."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=30_000):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]),
                _make_package(),
                "SD",
                product_config=_make_product_config(low_threshold=50_000, high_threshold=1_000_000),
            )
        assert result == round(0.10 * 1.5, 4)

    def test_custom_high_threshold_respected(self):
        """Custom high_threshold=100_000 — volume of 200_000 should trigger ×0.7."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=200_000):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]),
                _make_package(),
                "SD",
                product_config=_make_product_config(low_threshold=10_000, high_threshold=100_000),
            )
        assert result == round(0.10 * 0.7, 4)

    def test_none_thresholds_use_defaults(self):
        """product_config with None thresholds falls back to 10_000 / 1_000_000."""
        mgr = _make_manager()
        # Volume of 5_000 < default low (10_000) → ×1.5
        with patch.object(mgr, "_get_zone_volume", return_value=5_000):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]),
                _make_package(),
                "SD",
                product_config=_make_product_config(low_threshold=None, high_threshold=None),
            )
        assert result == round(0.10 * 1.5, 4)

    def test_no_product_config_uses_defaults(self):
        """No product_config argument → defaults apply (10_000 / 1_000_000)."""
        mgr = _make_manager()
        # Volume of 5_000 < default low (10_000) → ×1.5
        with patch.object(mgr, "_get_zone_volume", return_value=5_000):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]), _make_package(), "SD"
            )
        assert result == round(0.10 * 1.5, 4)


# ---------------------------------------------------------------------------
# _derive_starting_bid — Unknown campaign_type fallback
# ---------------------------------------------------------------------------

class TestUnknownCampaignType:
    def test_unknown_campaign_type_falls_back_to_005(self):
        """An unrecognised campaign_type string falls back to 0.05 (BASE_BIDS default)."""
        mgr = _make_manager()
        with patch.object(mgr, "_get_zone_volume", return_value=None):
            result = mgr._derive_starting_bid(
                _make_request(countries=["US"]), _make_package(), "UNKNOWN"
            )
        assert result == 0.05


# ---------------------------------------------------------------------------
# _get_zone_volume — graceful degradation
# ---------------------------------------------------------------------------

class TestGetZoneVolume:
    # _get_zone_volume uses a lazy import inside the method body, so we patch
    # get_db_session at its source module (the canonical import location).
    _DB_SESSION_PATH = "src.core.database.database_session.get_db_session"

    def _make_mock_ctx(self, rows: list) -> MagicMock:
        """Return a context-manager mock whose session returns the given rows."""
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = rows
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        return mock_ctx

    def test_returns_none_when_no_mappings(self):
        """No ProductInventoryMapping rows → None."""
        mgr = _make_manager()
        with patch(self._DB_SESSION_PATH, return_value=self._make_mock_ctx([])):
            result = mgr._get_zone_volume("prod-1")
        assert result is None

    def test_returns_none_when_db_raises(self):
        """DB error → None (graceful degradation, no exception propagated)."""
        mgr = _make_manager()
        with patch(self._DB_SESSION_PATH, side_effect=RuntimeError("DB unavailable")):
            result = mgr._get_zone_volume("prod-1")
        assert result is None

    def test_returns_none_when_rows_have_no_zone_stats(self):
        """Rows without zone_stats attribute → None (model column not yet added)."""
        mgr = _make_manager()
        row = SimpleNamespace(product_id="prod-1", inventory_id="zone-1")  # no zone_stats
        with patch(self._DB_SESSION_PATH, return_value=self._make_mock_ctx([row])):
            result = mgr._get_zone_volume("prod-1")
        assert result is None

    def test_sums_query_volume_across_zones(self):
        """Multiple rows with zone_stats → sum of query_volume values."""
        mgr = _make_manager()
        row1 = SimpleNamespace(zone_stats={"query_volume": 30_000})
        row2 = SimpleNamespace(zone_stats={"query_volume": 20_000})
        row3 = SimpleNamespace(zone_stats={"query_volume": 0})
        with patch(self._DB_SESSION_PATH, return_value=self._make_mock_ctx([row1, row2, row3])):
            result = mgr._get_zone_volume("prod-1")
        assert result == 50_000

    def test_returns_none_when_total_volume_is_zero(self):
        """All rows have query_volume=0 → None (treated as no data)."""
        mgr = _make_manager()
        row = SimpleNamespace(zone_stats={"query_volume": 0})
        with patch(self._DB_SESSION_PATH, return_value=self._make_mock_ctx([row])):
            result = mgr._get_zone_volume("prod-1")
        assert result is None
