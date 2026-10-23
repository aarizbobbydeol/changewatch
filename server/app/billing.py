"""Billing provider interface and no-op implementation.

Usage Metering Addendum Section G:
Defines the BillingProvider protocol for future payment adapters (e.g. Stripe, Razorpay).
No payment SDKs or external billing services are imported or executed here.
NoopBilling is the active default.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
import logging

logger = logging.getLogger("changewatch.billing")


@runtime_checkable
class BillingProvider(Protocol):
    """Protocol that future payment gateways or billing providers must satisfy."""

    async def report_usage(self, batch: List[Dict[str, Any]]) -> bool:
        """Report a batch of billable usage events to the billing provider.
        Each item contains: project_id, meter, units, billed_micros, occurred_at, idempotency_key.
        """
        ...

    async def get_balance(self, project_id: int) -> Optional[int]:
        """Retrieve the prepaid balance or remaining credit in integer micro-USD.
        Returns None if account is post-paid or unmanaged.
        """
        ...

    async def on_budget_event(self, event: Dict[str, Any]) -> None:
        """Hook called when a project hits a budget threshold (50%, 80%, 100%) or hard limit.
        Event contains: project_id, threshold_pct, spent_micros, limit_micros, hard_limit_hit.
        """
        ...


class NoopBilling:
    """Default no-op billing provider used in open-source and self-hosted deployments."""

    async def report_usage(self, batch: List[Dict[str, Any]]) -> bool:
        logger.debug("NoopBilling: report_usage called with %d items (noop)", len(batch))
        return True

    async def get_balance(self, project_id: int) -> Optional[int]:
        return None

    async def on_budget_event(self, event: Dict[str, Any]) -> None:
        logger.debug("NoopBilling: on_budget_event called for project %s (noop)", event.get("project_id"))


_billing_provider: BillingProvider = NoopBilling()


def get_billing_provider() -> BillingProvider:
    """Get the active billing provider instance."""
    return _billing_provider


def set_billing_provider(provider: BillingProvider) -> None:
    """Register an alternative billing provider implementation."""
    global _billing_provider
    _billing_provider = provider
