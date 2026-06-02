"""Siteplug SSP Adapter.

Adapter for the Siteplug SSP Tech API supporting search, native, and display
advertising with CPC, CPM, and flat-rate pricing.
"""

from .adapter import SiteplugAdapter
from .client import SiteplugAPIError, SiteplugClient
from .config_schema import SiteplugConnectionConfig, SiteplugProductConfig

__all__ = [
    "SiteplugAdapter",
    "SiteplugAPIError",
    "SiteplugClient",
    "SiteplugConnectionConfig",
    "SiteplugProductConfig",
]
