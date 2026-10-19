"""Fully loaded residual human work, versus an all-human baseline.

Hours follow the Last Human Gate accounting identity: cases people still
handle, verification, correction of errors that ship, and maintenance are
all inside the total. A case sits in exactly one handling bucket.

    H_agent = n_review * t_review
            + n_exception * t_exception
            + n_dual * t_dual
            + n_audit * t_audit
            + n_shipped_error * t_correction
            + maintenance

    H_human = n * t_human + human_maintenance

Shipped errors are autonomous misses plus, when human accuracy is below 1,
the expected reviewer mistakes on handled cases. Dollar cost adds model spend.
The breakeven volume is the smallest case count at which variable savings
pay for fixed maintenance. Parameters are the customer's, not a default ROI.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from breakeven.models import CostModel

ACCEPT = 0
REVIEW = 1
DUAL = 2
AUDIT = 3


@dataclass(frozen=True)
class Workload:
    n: int
    n_review: int
    n_exception: int
    n_dual: int
    n_audit: int
    n_accept: int
    n_miss: int
    n_handled: int
    hours: float
    baseline_hours: float
    dollars: float
    baseline_dollars: float
    model_dollars: float
    breakeven_cases: float | None

    @property
    def savings_hours(self) -> float:
        return self.baseline_hours - self.hours

    @property
    def savings_dollars(self) -> float:
        return self.baseline_dollars - self.dollars

    @property
    def hours_ratio(self) -> float:
        if self.baseline_hours == 0:
            return float("nan")
        return self.hours / self.baseline_hours


def workload_from_counts(
    *,
    n_sample: int,
    n_review: int,
    n_exception: int,
    n_dual: int,
    n_audit: int,
    n_accept: int,
    n_miss: int,
    model_dollars_sample: float,
    cost: CostModel,
    n_period: int,
) -> Workload:
    """Project sample handling rates onto a period of ``n_period`` cases.

    Maintenance is a fixed period cost and is not scaled with the sample
    fraction. Variable hours and model spend scale with volume.
    """
    if n_sample < 0 or n_period < 0:
        raise ValueError("counts must be non-negative")
    n_handled = n_review + n_exception + n_dual + n_audit
    expected_human_errors = n_handled * (1.0 - cost.human_accuracy)
    scale = (n_period / n_sample) if n_sample else 0.0
    variable_hours = scale * (
        n_review * cost.hours_per_review
        + n_exception * cost.hours_per_exception
        + n_dual * cost.hours_per_dual_review
        + n_audit * cost.hours_per_audit
        + n_miss * cost.hours_per_correction
        + expected_human_errors * cost.hours_per_correction
    )
    hours = variable_hours + cost.maintenance_hours
    baseline_hours = n_period * cost.hours_per_case_human + cost.human_maintenance_hours
    model_dollars = (model_dollars_sample / n_sample * n_period) if n_sample else 0.0
    # Prefer the customer's per-case model price when they set one; otherwise
    # use whatever the shadow log recorded.
    if cost.model_cost_per_case > 0:
        model_dollars = cost.model_cost_per_case * n_period
    dollars = hours * cost.hourly_rate + model_dollars
    baseline_dollars = baseline_hours * cost.hourly_rate
    breakeven = breakeven_volume(
        variable_hours_per_case=(variable_hours / n_period) if n_period else 0.0,
        human_hours_per_case=cost.hours_per_case_human,
        maintenance_hours=cost.maintenance_hours,
        human_maintenance_hours=cost.human_maintenance_hours,
        hourly_rate=cost.hourly_rate,
        model_cost_per_case=(model_dollars / n_period) if n_period else 0.0,
    )
    return Workload(
        n=n_period,
        n_review=n_review,
        n_exception=n_exception,
        n_dual=n_dual,
        n_audit=n_audit,
        n_accept=n_accept,
        n_miss=n_miss,
        n_handled=n_handled,
        hours=hours,
        baseline_hours=baseline_hours,
        dollars=dollars,
        baseline_dollars=baseline_dollars,
        model_dollars=model_dollars,
        breakeven_cases=breakeven,
    )


def breakeven_volume(
    *,
    variable_hours_per_case: float,
    human_hours_per_case: float,
    maintenance_hours: float,
    human_maintenance_hours: float = 0.0,
    hourly_rate: float,
    model_cost_per_case: float = 0.0,
) -> float | None:
    """Smallest volume where agent dollars drop below the all-human baseline.

    Closed form, with maintenance fixed and everything else linear in volume:

        N* = (maintenance - human_maintenance) * rate
             / (human_variable_dollars - agent_variable_dollars)

    ``None`` when variable agent cost is not strictly cheaper, so no volume
    breaks even.
    """
    agent_variable = variable_hours_per_case * hourly_rate + model_cost_per_case
    human_variable = human_hours_per_case * hourly_rate
    gap = human_variable - agent_variable
    fixed = (maintenance_hours - human_maintenance_hours) * hourly_rate
    if gap <= 0:
        return None
    if fixed <= 0:
        return 0.0
    return fixed / gap


def count_routes(
    route: np.ndarray,
    correct: np.ndarray,
    exception: np.ndarray,
) -> dict[str, int]:
    """Mutually exclusive handling counts. Exception time replaces review time."""
    route = np.asarray(route)
    correct = np.asarray(correct, dtype=bool)
    exception = np.asarray(exception, dtype=bool)
    dual = route == DUAL
    audit = route == AUDIT
    review = route == REVIEW
    accept = route == ACCEPT
    exception_review = review & exception
    standard_review = review & ~exception
    miss = accept & ~correct
    return {
        "n_review": int(standard_review.sum()),
        "n_exception": int(exception_review.sum()),
        "n_dual": int(dual.sum()),
        "n_audit": int(audit.sum()),
        "n_accept": int(accept.sum()),
        "n_miss": int(miss.sum()),
        "n_success_if_perfect_human": int((~accept | correct).sum()),
        "n_accepted_success": int((accept & correct).sum()),
        "n_accepted": int(accept.sum()),
    }


def curve(
    workload_rates: dict[str, float],
    cost: CostModel,
    volumes: np.ndarray,
) -> dict[str, list[float]]:
    """Dollar curves. ``workload_rates`` are per-case expected counts (rates)."""
    agent = []
    human = []
    for n in volumes:
        n = int(n)
        variable = n * (
            workload_rates["review"] * cost.hours_per_review
            + workload_rates["exception"] * cost.hours_per_exception
            + workload_rates["dual"] * cost.hours_per_dual_review
            + workload_rates["audit"] * cost.hours_per_audit
            + workload_rates["miss"] * cost.hours_per_correction
            + workload_rates["handled"] * (1.0 - cost.human_accuracy) * cost.hours_per_correction
        )
        model = n * cost.model_cost_per_case
        agent.append(float((variable + cost.maintenance_hours) * cost.hourly_rate + model))
        human.append(
            float((n * cost.hours_per_case_human + cost.human_maintenance_hours) * cost.hourly_rate)
        )
    return {
        "volume": [int(v) for v in volumes],
        "agent": agent,
        "human": human,
    }


def tornado(
    rates: dict[str, float],
    cost: CostModel,
    n_period: int,
    *,
    swing: float = 0.30,
) -> list[dict[str, float | str]]:
    """One-at-a-time ±swing impact on dollar savings. Sorted by absolute swing."""
    base = _dollars(rates, cost, n_period)
    base_human = (n_period * cost.hours_per_case_human + cost.human_maintenance_hours) * cost.hourly_rate
    base_savings = base_human - base
    fields = [
        ("hours_per_review", "Review time"),
        ("hours_per_exception", "Exception time"),
        ("hours_per_dual_review", "Dual-review time"),
        ("hours_per_audit", "Audit time"),
        ("hours_per_correction", "Correction time"),
        ("hours_per_case_human", "All-human time"),
        ("maintenance_hours", "Maintenance"),
        ("hourly_rate", "Hourly rate"),
        ("model_cost_per_case", "Model cost / case"),
    ]
    rows = []
    for field, label in fields:
        low_cost = cost.model_copy(update={field: max(0.0, getattr(cost, field) * (1.0 - swing))})
        high_cost = cost.model_copy(update={field: getattr(cost, field) * (1.0 + swing)})
        # Hourly rate of 0 would break the model; the field constraint is gt=0.
        if field == "hourly_rate":
            low_cost = cost.model_copy(update={field: cost.hourly_rate * (1.0 - swing)})
        low_savings = _savings(rates, low_cost, n_period)
        high_savings = _savings(rates, high_cost, n_period)
        rows.append(
            {
                "id": field,
                "label": label,
                "low": low_savings,
                "high": high_savings,
                "base": base_savings,
                "span": abs(high_savings - low_savings),
            }
        )
    rows.sort(key=lambda row: float(row["span"]), reverse=True)
    return rows


def _dollars(rates: dict[str, float], cost: CostModel, n_period: int) -> float:
    variable = n_period * (
        rates["review"] * cost.hours_per_review
        + rates["exception"] * cost.hours_per_exception
        + rates["dual"] * cost.hours_per_dual_review
        + rates["audit"] * cost.hours_per_audit
        + rates["miss"] * cost.hours_per_correction
        + rates["handled"] * (1.0 - cost.human_accuracy) * cost.hours_per_correction
    )
    model = n_period * cost.model_cost_per_case
    return (variable + cost.maintenance_hours) * cost.hourly_rate + model


def _savings(rates: dict[str, float], cost: CostModel, n_period: int) -> float:
    human = (n_period * cost.hours_per_case_human + cost.human_maintenance_hours) * cost.hourly_rate
    return human - _dollars(rates, cost, n_period)
