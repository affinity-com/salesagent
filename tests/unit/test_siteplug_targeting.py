"""Unit tests for SiteplugTargetingManager (Task 07).

Covers all 11 acceptance criteria from plans/siteplug-adapter/siteplug-task07.md:

build_targeting:
  AC1  {"geo_countries": ["US", "GB"]}  → {"country_codes": "us,gb"}
  AC2  {"device_type": "desktop"}       → {"device_targeting": "Desktop"}
  AC3  {"device_type": "all"}           → {"device_targeting": "Both"}
  AC4  {}                               → {}

validate_targeting:
  AC5  {"geo_countries": ["US"]}        → [] (valid)
  AC6  {"geo_countries": ["ZZZ"]}       → error with INVALID_GEO_COUNTRY
  AC7  {"device_type": "smarttv"}       → error with INVALID_DEVICE_TYPE
  AC8  {"geo_regions": ["US-CA"]}       → error with TARGETING_NOT_SUPPORTED
  AC9  {"geo_metros": [...]}            → error with TARGETING_NOT_SUPPORTED
  AC10 {"collection_list_exclude": [...]} → error with TARGETING_NOT_SUPPORTED

get_targeting_capabilities (adapter-level):
  AC11 geo_countries=True, geo_regions=False, geo_metros not declared
"""

import pytest

from src.adapters.siteplug.managers.targeting import SiteplugTargetingManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    """SiteplugTargetingManager with a null client (not needed for these tests)."""
    return SiteplugTargetingManager(client=None)


# ---------------------------------------------------------------------------
# build_targeting — AC1–AC4
# ---------------------------------------------------------------------------

class TestBuildTargeting:
    """Covers: build_targeting() field mapping and omission rules."""

    def test_ac1_geo_countries_comma_separated_lowercase(self, manager):
        """AC1: geo_countries list → country_codes comma-separated lowercase."""
        result = manager.build_targeting({"geo_countries": ["US", "GB"]})
        assert result == {"country_codes": "us,gb"}

    def test_geo_countries_single(self, manager):
        """Single country produces no trailing comma."""
        result = manager.build_targeting({"geo_countries": ["DE"]})
        assert result == {"country_codes": "de"}

    def test_geo_countries_already_lowercase(self, manager):
        """Already-lowercase codes are preserved."""
        result = manager.build_targeting({"geo_countries": ["us", "ca"]})
        assert result == {"country_codes": "us,ca"}

    def test_ac2_device_type_desktop(self, manager):
        """AC2: device_type 'desktop' → device_targeting 'Desktop'."""
        result = manager.build_targeting({"device_type": "desktop"})
        assert result == {"device_targeting": "Desktop"}

    def test_device_type_mobile(self, manager):
        """device_type 'mobile' → 'Mobile'."""
        result = manager.build_targeting({"device_type": "mobile"})
        assert result == {"device_targeting": "Mobile"}

    def test_device_type_tablet(self, manager):
        """device_type 'tablet' → 'Tablet'."""
        result = manager.build_targeting({"device_type": "tablet"})
        assert result == {"device_targeting": "Tablet"}

    def test_ac3_device_type_all_maps_to_both(self, manager):
        """AC3: device_type 'all' → device_targeting 'Both'."""
        result = manager.build_targeting({"device_type": "all"})
        assert result == {"device_targeting": "Both"}

    def test_ac4_empty_overlay_returns_empty_dict(self, manager):
        """AC4: empty overlay → empty dict (no defaults injected)."""
        result = manager.build_targeting({})
        assert result == {}

    def test_absent_geo_countries_omits_country_codes(self, manager):
        """Absent geo_countries → country_codes omitted (SSP API defaults to all)."""
        result = manager.build_targeting({"device_type": "mobile"})
        assert "country_codes" not in result

    def test_absent_device_type_omits_device_targeting(self, manager):
        """Absent device_type → device_targeting omitted (SSP API defaults to all)."""
        result = manager.build_targeting({"geo_countries": ["US"]})
        assert "device_targeting" not in result

    def test_empty_geo_countries_list_omits_country_codes(self, manager):
        """Empty geo_countries list → country_codes omitted."""
        result = manager.build_targeting({"geo_countries": []})
        assert "country_codes" not in result

    def test_both_fields_present(self, manager):
        """Both geo_countries and device_type produce both output fields."""
        result = manager.build_targeting({"geo_countries": ["US", "CA"], "device_type": "mobile"})
        assert result == {"country_codes": "us,ca", "device_targeting": "Mobile"}

    def test_unknown_fields_are_ignored(self, manager):
        """Fields not handled by Task 07 are silently ignored in build_targeting."""
        result = manager.build_targeting({
            "geo_countries": ["US"],
            "keyword_targets": [{"keyword": "shoes", "match_type": "broad"}],
        })
        assert result == {"country_codes": "us"}


# ---------------------------------------------------------------------------
# validate_targeting — AC5–AC10
# ---------------------------------------------------------------------------

class TestValidateTargeting:
    """Covers: validate_targeting() validation rules and error codes."""

    def test_ac5_valid_geo_country_returns_empty(self, manager):
        """AC5: valid ISO 3166-1 alpha-2 country → no errors."""
        errors = manager.validate_targeting({"geo_countries": ["US"]})
        assert errors == []

    def test_valid_multiple_countries(self, manager):
        """Multiple valid countries → no errors."""
        errors = manager.validate_targeting({"geo_countries": ["US", "GB", "DE"]})
        assert errors == []

    def test_ac6_invalid_country_code_three_letters(self, manager):
        """AC6: 3-letter code → INVALID_GEO_COUNTRY error."""
        errors = manager.validate_targeting({"geo_countries": ["ZZZ"]})
        assert len(errors) == 1
        assert "INVALID_GEO_COUNTRY" in errors[0]
        assert "ZZZ" in errors[0]

    def test_invalid_country_code_numeric(self, manager):
        """Numeric string → INVALID_GEO_COUNTRY error."""
        errors = manager.validate_targeting({"geo_countries": ["123"]})
        assert any("INVALID_GEO_COUNTRY" in e for e in errors)

    def test_invalid_country_code_single_letter(self, manager):
        """Single letter → INVALID_GEO_COUNTRY error."""
        errors = manager.validate_targeting({"geo_countries": ["U"]})
        assert any("INVALID_GEO_COUNTRY" in e for e in errors)

    def test_mixed_valid_and_invalid_countries(self, manager):
        """One valid + one invalid → exactly one error for the invalid one."""
        errors = manager.validate_targeting({"geo_countries": ["US", "ZZZ"]})
        assert len(errors) == 1
        assert "ZZZ" in errors[0]

    def test_ac7_invalid_device_type(self, manager):
        """AC7: unsupported device_type → INVALID_DEVICE_TYPE error."""
        errors = manager.validate_targeting({"device_type": "smarttv"})
        assert len(errors) == 1
        assert "INVALID_DEVICE_TYPE" in errors[0]
        assert "smarttv" in errors[0]

    def test_valid_device_types_no_error(self, manager):
        """All four valid device_type values produce no errors."""
        for dtype in ("desktop", "mobile", "tablet", "all"):
            errors = manager.validate_targeting({"device_type": dtype})
            assert errors == [], f"Expected no errors for device_type={dtype!r}"

    def test_ac8_geo_regions_rejected(self, manager):
        """AC8: geo_regions present → TARGETING_NOT_SUPPORTED error."""
        errors = manager.validate_targeting({"geo_regions": ["US-CA"]})
        assert len(errors) == 1
        assert "TARGETING_NOT_SUPPORTED" in errors[0]
        assert "geo_regions" in errors[0]

    def test_ac9_geo_metros_rejected(self, manager):
        """AC9: geo_metros present → TARGETING_NOT_SUPPORTED error."""
        errors = manager.validate_targeting({
            "geo_metros": [{"system": "nielsen_dma", "values": ["501"]}]
        })
        assert len(errors) == 1
        assert "TARGETING_NOT_SUPPORTED" in errors[0]
        assert "geo_metros" in errors[0]

    def test_ac10_collection_list_exclude_rejected(self, manager):
        """AC10: collection_list_exclude present → TARGETING_NOT_SUPPORTED error."""
        errors = manager.validate_targeting({
            "collection_list_exclude": [{"agent_url": "https://example.com", "list_id": "v1"}]
        })
        assert len(errors) == 1
        assert "TARGETING_NOT_SUPPORTED" in errors[0]
        assert "collection_list_exclude" in errors[0]

    def test_collection_list_rejected(self, manager):
        """collection_list present → TARGETING_NOT_SUPPORTED error."""
        errors = manager.validate_targeting({
            "collection_list": {"agent_url": "https://example.com", "list_id": "v1"}
        })
        assert any("TARGETING_NOT_SUPPORTED" in e and "collection_list" in e for e in errors)

    def test_signal_targeting_rejected(self, manager):
        """signal_targeting present → TARGETING_NOT_SUPPORTED error."""
        errors = manager.validate_targeting({"signal_targeting": {"provider": "x", "segments": ["a"]}})
        assert any("TARGETING_NOT_SUPPORTED" in e and "signal_targeting" in e for e in errors)

    def test_signal_targeting_groups_rejected(self, manager):
        """signal_targeting_groups present → TARGETING_NOT_SUPPORTED error."""
        errors = manager.validate_targeting({"signal_targeting_groups": [{"segments": ["a"]}]})
        assert any("TARGETING_NOT_SUPPORTED" in e and "signal_targeting_groups" in e for e in errors)

    def test_empty_overlay_is_valid(self, manager):
        """Empty overlay → no errors."""
        assert manager.validate_targeting({}) == []

    def test_multiple_errors_accumulated(self, manager):
        """Multiple violations → one error per violation."""
        errors = manager.validate_targeting({
            "geo_regions": ["US-CA"],
            "geo_metros": [{"system": "nielsen_dma", "values": ["501"]}],
            "geo_countries": ["ZZZ"],
            "device_type": "smarttv",
        })
        codes = [e.split(":")[0] for e in errors]
        assert codes.count("TARGETING_NOT_SUPPORTED") == 2
        assert "INVALID_GEO_COUNTRY" in codes
        assert "INVALID_DEVICE_TYPE" in codes

    def test_empty_list_for_unsupported_field_is_not_rejected(self, manager):
        """An unsupported field present as an empty list is not rejected
        (no constraint to honour — SSP API would ignore it anyway)."""
        errors = manager.validate_targeting({"geo_regions": []})
        assert errors == []


# ---------------------------------------------------------------------------
# get_targeting_capabilities — AC11
# ---------------------------------------------------------------------------

class TestGetTargetingCapabilities:
    """AC11: adapter declares geo_countries=True, geo_regions=False."""

    def test_ac11_geo_countries_true_geo_regions_false(self):
        """AC11: get_targeting_capabilities() declares correct geo flags."""
        from unittest.mock import MagicMock

        from src.adapters.siteplug.adapter import SiteplugAdapter
        from src.adapters.base import TargetingCapabilities

        config = {
            "base_url": "https://api.siteplug.com/ssp/v1",
            "api_key": "test-key",
            "affilizz_internal_url": "",
            "affilizz_api_key": "",
        }
        principal = MagicMock()
        principal.name = "test"
        principal.principal_id = "p1"

        adapter = SiteplugAdapter(
            config=config,
            principal=principal,
            dry_run=True,
            tenant_id="test-tenant",
        )
        caps = adapter.get_targeting_capabilities()

        assert isinstance(caps, TargetingCapabilities)
        assert caps.geo_countries is True
        assert caps.geo_regions is False
        # Keyword fields preserved (Task 12 owns these)
        assert caps.keyword_targets == ["broad", "phrase", "exact"]
        assert caps.negative_keywords == ["broad", "exact"]
