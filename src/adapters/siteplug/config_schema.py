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
