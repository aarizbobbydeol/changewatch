# ChangeWatch Pricing Model & Metering Architecture

ChangeWatch is open-source and self-hosted. For hosted or multi-tenant deployments, ChangeWatch implements **cost-plus pay-as-you-go metering with strict budget caps**.

The guiding principle is transparency and predictability:
- **Price = Measured Infrastructure Cost + Small Configurable Margin**
- **All financial amounts are stored as integer micro-USD** (\(1\text{ USD} = 1,000,000\text{ micros}\)) to prevent floating-point rounding errors.
- **The Safety Floor is NEVER metered, billed, or paused.**

---

## 1. Non-Negotiable Invariant: The Safety Floor

The primary mission of ChangeWatch is to protect developers from broken releases. Therefore, the core safety mechanisms are completely free, unmetered, and exempt from budget limits:

| Feature | Metered? | Gated by Budget? |
|---|---|---|
| **Deploy Logging & Ingest** | No (Free) | Never |
| **Instant Error Alerts (< 5s)** | No (Free) | Never |
| **Release Verdicts (10m & 30m)** | No (Free) | Never |
| **Dead-Man's Switch (Silence Alerts)** | No (Free) | Never |
| **Watchdog & System Alerts** | No (Free) | Never |
| **Multi-Channel Notifications** | No (Free) | Never |
| **Dashboard Access & Inspection** | No (Free) | Never |
| **Automated AI Root-Cause Investigation** | **Yes** (Paid/Metered) | **Yes** (Paused at Hard Limit) |

Even if a project exhausts 100% of its budget hard cap, **instant alerts and release verdicts continue to function normally**. Only optional value-add features (such as autonomous AI investigations) are paused.

---

## 2. Active Meters & Default Price Book

Meters are defined in `server/app/usage.py` and stored in the database:

| Meter Name | Billable Unit | Included Free Allowance | Cost per Unit (Micros) | Margin | Price per Unit |
|---|---|---|---|---|---|
| `events_ingested` | 1,000 events | 100,000 events / month | 500 micros (\$0.0005) | 20% | 600 micros (\$0.0006) |
| `ai_investigation` | 1 investigation | 50 investigations / month | 25,000 micros (\$0.025) | 15% | 28,750 micros (\$0.02875) |
| `ai_tokens_input` | 1,000 tokens | 500,000 tokens / month | 3,000 micros (\$0.003) | 10% | 3,300 micros (\$0.0033) |
| `ai_tokens_output` | 1,000 tokens | 100,000 tokens / month | 15,000 micros (\$0.015) | 10% | 16,500 micros (\$0.0165) |
| `data_retention_days` | 1 project-day | 30 days included | 10,000 micros (\$0.010) | 0% | 10,000 micros (\$0.010) |

---

## 3. Cost-Plus Pricing Mechanics

1. **Daily Aggregation:** Usage events are batched into `usage_events` and rolled into `usage_daily` tables with tenant row-level security.
2. **Monthly Allowance Deduction:** During cost computation, free allowances are deducted first. Only excess consumption is billed.
3. **Forecast & Burn Rate:** The API provides a linear month-end projection via `GET /v1/usage/forecast` based on the 7-day rolling daily average burn rate. If fewer than 3 days of data exist, `not_enough_data` is returned.
4. **Pre-Flight Estimation:** Teams can estimate the cost of an action before running it via `GET /v1/usage/estimate?meter=ai_investigation&count=10`.

---

## 4. Budgets and Safety Gating

Budgets are configured per project:
- **Soft Warning Limits (e.g., 50%, 80%):** Triggers proactive notification alerts to configured Slack/Discord channels to prevent surprise bills.
- **Hard Limit (100%):** When reached, `can_spend(project_id, meter)` returns `False`.
  - Ingest continues unaffected.
  - Workers continue fingerprinting and opening incidents.
  - Instant alerts continue sending immediately.
  - The background AI investigator logs a budget-paused notice and skips the LLM call.
  - The dashboard displays a banner: `"AI investigation paused due to monthly budget limit. Core alerting remains active."`

---

## 5. How Future Billing Adapters Plug In

ChangeWatch ships with `NoopBilling` enabled by default (`server/app/billing.py`).

To connect a payment provider (such as Stripe, Razorpay, or an internal enterprise billing system), implement the `BillingProvider` protocol:

```python
from typing import Any, Dict, List, Optional
from app.billing import BillingProvider, set_billing_provider

class StripeBillingProvider:
    async def report_usage(self, batch: List[Dict[str, Any]]) -> bool:
        """Send metered billing records to Stripe Meter Events API."""
        for item in batch:
            # stripe.billing.meter_events.create(...)
            pass
        return True

    async def get_balance(self, project_id: int) -> Optional[int]:
        """Fetch customer prepaid balance or return None for post-paid."""
        # customer = stripe.Customer.retrieve(...)
        return 50000000  # $50.00 in micro-USD

    async def on_budget_event(self, event: Dict[str, Any]) -> None:
        """Handle budget limit thresholds (e.g. invoice creation or warning emails)."""
        logger.info("Project %s crossed threshold %s%%", event["project_id"], event["threshold_pct"])

# Register the provider on startup:
set_billing_provider(StripeBillingProvider())
```

No core ingestion, worker, detection, or alerting code needs to be modified when plugging in an external billing adapter.
