"""Siteplug adapter managers package."""

from .campaign import SiteplugCampaignManager
from .creative import SiteplugCreativeManager
from .inventory import SiteplugInventoryManager
from .reporting import SiteplugReportingManager
from .targeting import SiteplugTargetingManager
from .workflow import SiteplugWorkflowManager

__all__ = [
    "SiteplugCampaignManager",
    "SiteplugCreativeManager",
    "SiteplugInventoryManager",
    "SiteplugReportingManager",
    "SiteplugTargetingManager",
    "SiteplugWorkflowManager",
]
