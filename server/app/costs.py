"""Cost tracking (backlog item): what the customer's own AI calls cost per release, and what an
incident cost. Money is integer micro-USD (1 USD = 1,000,000), converted with Decimal only for
display; never floats.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

import asyncpg

from app.config import settings

# A release is called out when AI spend per request (or per call, without traffic) at least
# doubles, on enough calls and money that it is not noise
AI_COST_JUMP_RATIO = Decimal(2)
AI_COST_MIN_CALLS = 20
AI_COST_MIN_MICROS = 100_000  # $0.10 after the deploy


def usd(micros: Optional[int]) -> Optional[str]:
    """'$1.23' from integer micro-USD; amounts under 10 cents that are not whole cents keep four
    decimals ('$0.0042'), so a small per-request cost does not show as $0.00."""
    if micros is None:
        return None
    value = Decimal(micros) / Decimal(1_000_000)
    cents = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(value) >= Decimal("0.10") or value == cents:
        return f"${cents}"
    return f"${value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)}"


def price_ai_call(model: str, input_tokens: int, output_tokens: int) -> Optional[int]:
    """Cost in micro-USD from LLM_PRICES_JSON (USD per million tokens), or None when the model
    has no configured price (it is counted, but not guessed at)."""
    try:
        rates = json.loads(settings.LLM_PRICES_JSON, parse_float=Decimal).get(model)
    except (ValueError, AttributeError):
        rates = None
    if not isinstance(rates, dict):
        return None
    total = Decimal(max(0, input_tokens)) * Decimal(rates.get("input", 0)) + Decimal(max(0, output_tokens)) * Decimal(rates.get("output", 0))
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))


async def _window(conn: asyncpg.Connection, project_id: int, env: str, deploy_id: int, start: datetime, end: datetime,
                  after: bool) -> Dict[str, Any]:
    """Calls, cost and requests in a window. "After" includes rows the SDK tagged with this
    release (the deploy's own minute starts before deployed_at), "before" excludes them."""
    tag = "deploy_id = $5 OR bucket >= $3" if after else "deploy_id IS DISTINCT FROM $5 AND bucket >= $3"
    usage = await conn.fetchrow(
        f"""
        SELECT coalesce(sum(calls), 0) AS calls, coalesce(sum(cost_micros), 0) AS cost,
               coalesce(sum(unpriced_calls), 0) AS unpriced,
               coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens
        FROM ai_usage
        WHERE project_id = $1 AND environment = $2 AND bucket < $4 AND ({tag})
        """,
        project_id, env, start, end, deploy_id,
    )
    requests = await conn.fetchval(
        f"""
        SELECT coalesce(sum(requests), 0) FROM traffic
        WHERE project_id = $1 AND environment = $2 AND bucket < $4 AND ({tag})
        """,
        project_id, env, start, end, deploy_id,
    )
    return {
        "calls": int(usage["calls"]), "cost_micros": int(usage["cost"]), "unpriced_calls": int(usage["unpriced"]),
        "input_tokens": int(usage["input_tokens"]), "output_tokens": int(usage["output_tokens"]),
        "requests": int(requests or 0),
    }


def _per(total: int, count: int, unit: int = 1) -> Optional[int]:
    """Integer micro-USD per `unit` items (rounded down, for display), or None without items."""
    return total * unit // count if count > 0 else None


def _change(before_cost: int, before_count: int, after_cost: int, after_count: int) -> Decimal:
    """The exact ratio of cost per item after to before, from the raw totals (the rounded-down
    per-item figures can turn 1.9 and 2.0 micros into 1 and 2, a '2x' that is really 5%)."""
    return Decimal(after_cost * before_count) / Decimal(before_cost * after_count)


async def ai_cost_for_deploy(conn: asyncpg.Connection, project_id: int, deploy_id: int) -> Optional[Dict[str, Any]]:
    """AI spend in the 30 minutes before the deploy and since it (up to 30 minutes or the next
    deploy): totals, per 1,000 requests, per call, the change, and the top models and features."""
    dep = await conn.fetchrow(
        """
        SELECT d.deployed_at, d.environment,
               (SELECT min(n.deployed_at) FROM deploys n WHERE n.project_id = d.project_id
                  AND n.environment = d.environment AND n.deployed_at > d.deployed_at) AS next_deployed_at
        FROM deploys d WHERE d.id = $1 AND d.project_id = $2
        """,
        deploy_id, project_id,
    )
    if not dep:
        return None
    env = dep["environment"] or "prod"
    deployed_at = dep["deployed_at"]
    end = min(datetime.now(timezone.utc), deployed_at + timedelta(minutes=30))
    if dep["next_deployed_at"]:
        end = min(end, dep["next_deployed_at"])
    before = await _window(conn, project_id, env, deploy_id, deployed_at - timedelta(minutes=30), deployed_at, after=False)
    after = await _window(conn, project_id, env, deploy_id, deployed_at, end + timedelta(minutes=1), after=True)
    # Calls to a model without a price add nothing to the cost, so "per call" leaves them out
    priced_before, priced_after = (w["calls"] - w["unpriced_calls"] for w in (before, after))
    for w, priced in ((before, priced_before), (after, priced_after)):
        w["per_1k_requests_micros"] = _per(w["cost_micros"], w["requests"], 1000)
        w["per_call_micros"] = _per(w["cost_micros"], priced)
        w["cost"] = usd(w["cost_micros"])
        w["per_1k_requests"] = usd(w["per_1k_requests_micros"])
        w["per_call"] = usd(w["per_call_micros"])

    # Not compared when half or more of either window's calls have no price: most of that cost is
    # unknown, and moving calls to a priced model would look like a jump
    basis, ratio = None, None
    known = all(w["unpriced_calls"] * 2 < w["calls"] for w in (before, after) if w["calls"] > 0)
    if known and before["cost_micros"] > 0:
        if before["requests"] > 0 and after["requests"] > 0:
            basis, ratio = "per_1k_requests", _change(before["cost_micros"], before["requests"], after["cost_micros"], after["requests"])
        elif priced_before > 0 and priced_after > 0:
            basis, ratio = "per_call", _change(before["cost_micros"], priced_before, after["cost_micros"], priced_after)

    top = await conn.fetch(
        """
        SELECT model, feature, sum(calls)::bigint AS calls, sum(cost_micros)::bigint AS cost_micros
        FROM ai_usage
        WHERE project_id = $1 AND environment = $2 AND bucket < $4 AND (deploy_id = $5 OR bucket >= $3)
        GROUP BY model, feature ORDER BY 4 DESC, 3 DESC LIMIT 5
        """,
        project_id, env, deployed_at, end + timedelta(minutes=1), deploy_id,
    )
    jumped = bool(
        ratio is not None and ratio >= AI_COST_JUMP_RATIO
        and priced_after >= AI_COST_MIN_CALLS and after["cost_micros"] >= AI_COST_MIN_MICROS
    )
    return {
        "deploy_id": deploy_id,
        "environment": env,
        "before": before,
        "after": after,
        "basis": basis,
        "change": f"{ratio.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}x" if ratio is not None else None,
        "jumped": jumped,
        "top": [{"model": r["model"], "feature": r["feature"] or None, "calls": r["calls"],
                 "cost_micros": r["cost_micros"], "cost": usd(r["cost_micros"])} for r in top],
    }


def ai_cost_jump_message(cost: Dict[str, Any], commit_sha: str) -> Dict[str, Any]:
    """The notification for a release whose AI spend jumped."""
    basis = "per 1,000 requests" if cost["basis"] == "per_1k_requests" else "per call"
    key = "per_1k_requests" if cost["basis"] == "per_1k_requests" else "per_call"
    top = cost["top"][0] if cost["top"] else None
    body = (
        f"AI spend {basis} went from {cost['before'][key]} to {cost['after'][key]} ({cost['change']}) "
        f"after deploy {commit_sha[:8]}: {cost['after']['cost']} on {cost['after']['calls']} calls so far."
        + (f" Most of it: {top['model']}" + (f" ({top['feature']})" if top["feature"] else "") + f", {top['cost']}." if top else "")
        + " Check for a changed prompt, model, retry loop or a call made per item instead of once."
    )
    return {"type": "ai_cost_jump", "title": f"AI spend up {cost['change']} after deploy {commit_sha[:8]}", "body": body}


async def incident_cost(conn: asyncpg.Connection, project_id: int, incident_id: int) -> Optional[Dict[str, Any]]:
    """What an incident cost: failed requests of this error, minutes it lasted, what ChangeWatch's
    AI investigations cost, and (when the project set a value per failed request) an estimate of
    the business impact."""
    inc = await conn.fetchrow(
        """
        SELECT i.issue_id, i.deploy_id, i.created_at, i.first_event_at, i.last_event_at, i.resolved_at,
               p.failed_request_value_micros, d.environment
        FROM incidents i JOIN projects p ON p.id = i.project_id JOIN deploys d ON d.id = i.deploy_id
        WHERE i.id = $1 AND i.project_id = $2
        """,
        incident_id, project_id,
    )
    if not inc:
        return None
    start = inc["first_event_at"] or inc["created_at"]
    end = inc["resolved_at"] or inc["last_event_at"] or datetime.now(timezone.utc)
    # Issues are shared by environments: count only this error's failures in the environment of
    # the incident's deploy (each error is tied to a deploy of its own environment), not staging's
    failed = await conn.fetchval(
        """
        SELECT coalesce(sum(r.count), 0) FROM event_rollups r
        JOIN deploys d ON d.id = r.deploy_id AND d.project_id = r.project_id
        WHERE r.project_id = $1 AND r.issue_id = $2 AND r.bucket >= $3 AND r.bucket <= $4 AND d.environment = $5
        """,
        project_id, inc["issue_id"], start.replace(second=0, microsecond=0), end, inc["environment"],
    )
    investigations: List[Any] = await conn.fetch(
        "SELECT model, input_tokens, output_tokens FROM investigations WHERE project_id = $1 AND incident_id = $2",
        project_id, incident_id,
    )
    ai_micros = 0
    for inv in investigations:
        price = price_ai_call(inv["model"] or "", int(inv["input_tokens"] or 0), int(inv["output_tokens"] or 0))
        ai_micros += price or 0
    value = inc["failed_request_value_micros"]
    impact = int(failed) * int(value) if value is not None else None
    return {
        "failed_requests": int(failed),
        "minutes": max(0, int((end - start).total_seconds() // 60)) if start else None,
        "investigation_cost_micros": ai_micros,
        "investigation_cost": usd(ai_micros),
        "value_per_failed_request": usd(value),
        "estimated_impact_micros": impact,
        "estimated_impact": usd(impact),
    }
