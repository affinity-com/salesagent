"""Siteplug targeting manager.

Translates AdCP targeting overlay fields into SSP API campaign parameters
for geo (country-level) and device targeting.

Scope (Task 07):
- geo_countries  → country_codes  (comma-separated lowercase ISO 3166-1 alpha-2)
- device_type    → device_targeting ("Desktop" | "Mobile" | "Tablet" | "Both")

Deferred:
- geo_regions / geo_metros  → Task 13 (blocked on SSP API enhancement)
- keyword_targets / negative_keywords → Task 12
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ISO 3166-1 alpha-2 format: exactly 2 ASCII letters
_ISO_ALPHA2_RE = re.compile(r"^[A-Za-z]{2}$")

# AdCP device_type → SSP API device_targeting string
_DEVICE_TYPE_MAP: dict[str, str] = {
    "desktop": "Desktop",
    "mobile": "Mobile",
    "tablet": "Tablet",
    "all": "Both",
}

# Capability-gated fields that must be rejected (not silently ignored)
# when present in the overlay — accepting without honouring is a
# delivery-integrity violation per AdCP protocol notes.
_UNSUPPORTED_FIELDS: tuple[str, ...] = (
    "geo_regions",
    "geo_metros",
    "signal_targeting",
    "signal_targeting_groups",
    "collection_list",
    "collection_list_exclude",
)


class SiteplugTargetingManager:
    """Translates AdCP targeting overlays into Siteplug SSP API parameters.

    Supports geo_countries and device_type (Task 07).
    Rejects unsupported capability-gated fields with explicit error codes.
    """

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialise the targeting manager.

        Args:
            client: SiteplugClient instance (unused in this manager but kept
                    for interface consistency with other managers).
            log_func: Optional logging function from the adapter.
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_targeting(self, overlay: dict[str, Any]) -> dict[str, Any]:
        """Translate an AdCP targeting overlay dict into SSP API parameters.

        Only fields supported in Task 07 are translated; all others are
        ignored at this stage (validation is the caller's responsibility).

        Args:
            overlay: AdCP targeting overlay as a plain dict.  Keys match
                     AdCP ``core/targeting.json`` field names.

        Returns:
            Dict of SSP API campaign parameters.  Only keys with non-empty
            values are included (absent fields → SSP API defaults).

        Examples::

            build_targeting({"geo_countries": ["US", "GB"]})
            # → {"country_codes": "us,gb"}

            build_targeting({"device_type": "desktop"})
            # → {"device_targeting": "Desktop"}

            build_targeting({})
            # → {}
        """
        result: dict[str, Any] = {}

        # geo_countries → country_codes (comma-separated lowercase)
        geo_countries = overlay.get("geo_countries")
        if geo_countries:
            codes = [str(c).lower() for c in geo_countries]
            if codes:
                result["country_codes"] = ",".join(codes)

        # device_type → device_targeting
        device_type = overlay.get("device_type")
        if device_type is not None:
            ssp_value = _DEVICE_TYPE_MAP.get(str(device_type).lower())
            if ssp_value:
                result["device_targeting"] = ssp_value

        return result

    def validate_targeting(self, overlay: dict[str, Any]) -> list[str]:
        """Validate an AdCP targeting overlay before building SSP parameters.

        Returns a list of human-readable error strings (empty = valid).
        Each string is prefixed with the error code so callers can surface
        structured information if needed.

        Validation rules (Task 07):
        - geo_countries: each entry must be a 2-letter string (format only,
          no DB lookup).
        - device_type: must be one of desktop | mobile | tablet | all.
        - geo_regions, geo_metros, signal_targeting, signal_targeting_groups,
          collection_list, collection_list_exclude: rejected if present
          (capability-gated, not yet supported).

        Args:
            overlay: AdCP targeting overlay as a plain dict.

        Returns:
            List of error strings.  Empty list means the overlay is valid.
        """
        errors: list[str] = []

        # ── Unsupported capability-gated fields ───────────────────────────
        for field in _UNSUPPORTED_FIELDS:
            value = overlay.get(field)
            # Reject if the field is present and non-empty
            if value is not None and value != [] and value != {}:
                errors.append(
                    f"TARGETING_NOT_SUPPORTED: '{field}' targeting is not supported "
                    f"by this adapter (deferred to a future task)"
                )

        # ── geo_countries ─────────────────────────────────────────────────
        geo_countries = overlay.get("geo_countries")
        if geo_countries is not None:
            for entry in geo_countries:
                code = str(entry)
                if not _ISO_ALPHA2_RE.match(code):
                    errors.append(
                        f"INVALID_GEO_COUNTRY: '{code}' is not a valid ISO 3166-1 "
                        f"alpha-2 country code (must be exactly 2 letters)"
                    )

        # ── device_type ───────────────────────────────────────────────────
        device_type = overlay.get("device_type")
        if device_type is not None:
            if str(device_type).lower() not in _DEVICE_TYPE_MAP:
                errors.append(
                    f"INVALID_DEVICE_TYPE: '{device_type}' is not a supported device "
                    f"type. Supported values: {', '.join(sorted(_DEVICE_TYPE_MAP))}"
                )

        return errors
