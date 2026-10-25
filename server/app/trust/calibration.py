"""Decision Layer Calibration and Reliability Analysis (TD Addendum Stage 6).

Computes predicted vs observed reliability tables, Brier score, and sample sizes
for typed decisions questions (is_regression_from_release, is_known_noise).
"""
from __future__ import annotations

import logging
from typing import Any, Dict
import asyncpg

logger = logging.getLogger("changewatch.trust.calibration")

BINS = [
    {"label": "0% - 20%", "min": 0.0, "max": 0.20, "midpoint": 0.10},
    {"label": "20% - 40%", "min": 0.20, "max": 0.40, "midpoint": 0.30},
    {"label": "40% - 60%", "min": 0.40, "max": 0.60, "midpoint": 0.50},
    {"label": "60% - 80%", "min": 0.60, "max": 0.80, "midpoint": 0.70},
    {"label": "80% - 100%", "min": 0.80, "max": 1.01, "midpoint": 0.90},
]


async def compute_decision_calibration(
    conn: asyncpg.Connection,
    project_id: int,
    question_id: str = "is_regression_from_release",
    min_bin_sample: int = 10,
) -> Dict[str, Any]:
    """
    Compute calibration reliability table and Brier score.
    Joins decisions with alert_feedback (real=1, false=0).
    """
    rows = await conn.fetch(
        """
        SELECT d.probability,
               CASE
                 WHEN af.label = 'real' THEN 1.0
                 WHEN af.label = 'false' THEN 0.0
                 WHEN i.confirmed_at IS NOT NULL THEN 1.0
                 ELSE NULL
               END AS outcome
        FROM decisions d
        JOIN incidents i ON i.id = d.incident_id AND i.project_id = d.project_id
        -- One outcome per incident however many people rated it: the latest real/false rating
        -- ('unclear' says nothing), else whether it was confirmed
        LEFT JOIN LATERAL (
            SELECT label FROM alert_feedback f
            WHERE f.incident_id = i.id AND f.project_id = i.project_id AND f.label IN ('real', 'false')
            ORDER BY f.created_at DESC
            LIMIT 1
        ) af ON true
        WHERE d.project_id = $1
          AND d.question_id = $2
          AND d.probability IS NOT NULL
        """,
        project_id,
        question_id,
    )

    data_points = []
    for r in rows:
        prob = float(r["probability"])
        out = r["outcome"]
        if out is not None:
            data_points.append((prob, float(out)))

    total_n = len(data_points)
    if total_n == 0:
        return {
            "question_id": question_id,
            "total_samples": 0,
            "brier_score": None,
            "bins": [],
            "status": "collecting_data",
            "headline": "Collecting calibration data (n=0)",
        }

    # Brier score: mean((prob - outcome)^2)
    brier_sum = sum((p - o) ** 2 for p, o in data_points)
    brier_score = round(brier_sum / total_n, 4)

    # Binning
    bin_results = []
    headline_bin_observed = None

    for b in BINS:
        bin_items = [o for p, o in data_points if b["min"] <= p < b["max"]]
        n = len(bin_items)
        if n >= min_bin_sample:
            observed_rate = round(sum(bin_items) / n, 3)
            visible = True
        else:
            observed_rate = None
            visible = False

        if b["label"] == "80% - 100%" and visible and observed_rate is not None:
            headline_bin_observed = int(round(observed_rate * 100))

        bin_results.append({
            "bin": b["label"],
            "midpoint": b["midpoint"],
            "samples": n,
            "observed_rate": observed_rate,
            "visible": visible,
        })

    headline = (
        f"When we said about 90%, we were right {headline_bin_observed}% of the time"
        if headline_bin_observed is not None
        else f"Collecting calibration data (n={total_n})"
    )

    return {
        "question_id": question_id,
        "total_samples": total_n,
        "brier_score": brier_score,
        "bins": bin_results,
        "status": "calibrated" if headline_bin_observed is not None else "collecting_data",
        "headline": headline,
    }
