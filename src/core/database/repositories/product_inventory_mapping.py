"""ProductInventoryMapping repository — tenant-scoped data access.

Encapsulates all ``select(ProductInventoryMapping)`` queries so that adapter
code never issues raw ORM selects directly.

Core invariant: every query includes ``tenant_id`` in the WHERE clause.
The ``tenant_id`` is set at construction time and injected automatically.

beads: salesagent-xw7 (migrate raw selects to repository calls)
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.models import ProductInventoryMapping

logger = logging.getLogger(__name__)


class ProductInventoryMappingRepository:
    """Tenant-scoped data access for :class:`~src.core.database.models.ProductInventoryMapping`.

    All queries filter by ``tenant_id`` automatically. Write methods add
    objects to the session but never commit — the caller manages the
    transaction boundary.

    Args:
        session: SQLAlchemy session (caller manages lifecycle).
        tenant_id: Tenant scope for all queries.
    """

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    # ------------------------------------------------------------------
    # Read queries
    # ------------------------------------------------------------------

    def list_by_product(
        self,
        product_id: str,
        inventory_type: str,
    ) -> list[ProductInventoryMapping]:
        """Return all mappings for a specific product and inventory type.

        Used by :meth:`SiteplugInventoryManager.get_zones` to scope the zone
        list to a single AdCP product.

        Args:
            product_id: AdCP product identifier.
            inventory_type: Inventory type string (e.g. ``"zone"``).

        Returns:
            List of :class:`ProductInventoryMapping` rows, possibly empty.
        """
        stmt = select(ProductInventoryMapping).where(
            ProductInventoryMapping.tenant_id == self._tenant_id,
            ProductInventoryMapping.product_id == product_id,
            ProductInventoryMapping.inventory_type == inventory_type,
        )
        return list(self._session.scalars(stmt).all())

    def list_by_catalog_product(
        self,
        catalog_product_id: str,
        inventory_type: str,
    ) -> list[ProductInventoryMapping]:
        """Return all mappings for the zone-catalogue sentinel product.

        Used by :meth:`SiteplugInventoryManager._upsert_zones` to fetch
        existing rows before computing adds/updates/deletes.

        Args:
            catalog_product_id: Sentinel product ID (e.g.
                ``"__siteplug_zone_catalog__"``).
            inventory_type: Inventory type string (e.g. ``"zone"``).

        Returns:
            List of :class:`ProductInventoryMapping` rows, possibly empty.
        """
        stmt = select(ProductInventoryMapping).where(
            ProductInventoryMapping.tenant_id == self._tenant_id,
            ProductInventoryMapping.product_id == catalog_product_id,
            ProductInventoryMapping.inventory_type == inventory_type,
        )
        return list(self._session.scalars(stmt).all())
