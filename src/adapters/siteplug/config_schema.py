"""Siteplug SSP adapter configuration schemas.

Defines the Pydantic models for Siteplug connection and product configuration.
"""

from pydantic import Field, field_validator

from src.adapters.base import BaseConnectionConfig, BaseProductConfig


class SiteplugConnectionConfig(BaseConnectionConfig):
    """Connection configuration for the Siteplug SSP Tech API."""

    base_url: str = Field(
        ...,
        description="SSP API base URL (e.g. https://api.siteplug.com/ssp/v1)",
    )
    api_key: str = Field(
        ...,
        description="X-API-Key for SSP API authentication",
    )
    timeout: int = Field(
        default=30,
        ge=1,
        description="HTTP request timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Max retry attempts for transient failures",
    )

    # Affilizz Internal Text Ads API — for siteplug_text_ad_search creative sync
    # These are ADDITIVE optional fields. The existing base_url + api_key fields
    # (Siteplug SSP API, X-API-Key header) are unchanged and unrelated.
    affilizz_internal_url: str = ""  # e.g. "https://api.affilizz.com/v1/ads-aura"
    affilizz_api_key: str = ""  # ApiKey header value (INTERNAL_API-scoped token)

    # Brand-agent integration — optional; used to fetch related TLD/geo domains
    # (T10 brand.json enrichment) for king domain whitelisting on SDC campaigns.
    # When set, the adapter calls GET /api/brands/{brand_id} on the brand-agent
    # and extracts website properties as additional king domains.
    brand_agent_url: str = ""   # e.g. "https://brand-agent.internal/api"
    brand_agent_api_key: str = ""  # X-API-Key for brand-agent REST API
    brand_agent_tenant_id: str = ""  # Tenant scope for brand-agent queries


class SiteplugProductConfig(BaseProductConfig):
    """Product-level configuration for Siteplug campaigns."""

    platform_id: int = Field(
        ...,
        description="Siteplug platform ID",
    )
    brand_id: int = Field(
        ...,
        description="Siteplug brand ID",
    )
    campaign_type: int = Field(
        default=1,
        description="Campaign type: 1=KW, 2=RON, 3=CAT, 4=HYBRID, 5=PLA",
    )
    pricing_model: str = Field(
        default="cpc",
        description="Pricing model: cpc, cpm, or flat_rate",
    )
    default_bid: float = Field(
        default=0.10,
        gt=0,
        description="Default CPC/CPM bid",
    )
    default_budget: float = Field(
        default=1000.0,
        gt=0,
        description="Default campaign budget",
    )

    @field_validator("pricing_model")
    @classmethod
    def validate_pricing_model(cls, v: str) -> str:
        """Validate pricing model is one of the supported values."""
        valid_models = {"cpc", "cpm", "flat_rate"}
        v_lower = v.lower()
        if v_lower not in valid_models:
            raise ValueError(f"Invalid pricing_model '{v}'. Must be one of: {valid_models}")
        return v_lower
