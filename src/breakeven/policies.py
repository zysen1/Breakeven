"""Oversight-policy search on the calibration split.

Families, matching the brief:

- autonomous: accept every agent decision
- threshold: send confidence below tau to a person
- sample_audit: check a deterministic fraction of cases
- two_key: a customer-supplied high-risk slice always gets a second reviewer
- state_check: a workflow-state assertion failure always escalates

The grid is built only from calibration cases and from the customer's risk
menu. Holdout cases are not an input to this module's search.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from breakeven.cost import (
    ACCEPT,
    AUDIT,
    DUAL,
    REVIEW,
    count_routes,
    workload_from_counts,
)
from breakeven.models import CostModel
from breakeven.stats import clopper_pearson_upper, reliability_bound

# Customer-supplied menu for the Acme template. These are prior review rules,
# not categories discovered on the qualification set.
DEFAULT_CATEGORY_SETS: tuple[tuple[str, ...], ...] = (
    ("health",),
    ("health", "liability"),
    ("liability",),
)
DEFAULT_AMOUNT_CUTOFFS: tuple[float | None, ...] = (None, 10_000.0, 25_000.0, 50_000.0)


@dataclass
class Batch:
    case_id: np.ndarray
    category: np.ndarray
    amount: np.ndarray
    exception: np.ndarray
    correct: np.ndarray
    confidence: np.ndarray
    violation: np.ndarray
    model_cost: np.ndarray

    def __len__(self) -> int:
        return int(self.confidence.size)


@dataclass
class Policy:
    policy_id: str
    family: str
    label: str
    params: dict
    reference: str | None = None
    dev_lcb: float = 0.0
    dev_point: float = 0.0
    dev_wilson: float = 0.0
    dev_dollars: float = 0.0
    selection_dollars: float = 0.0
    dev_hours: float = 0.0
    dev_counts: dict = field(default_factory=dict)


def _unit_interval(salt: str, case_id: str) -> float:
    digest = hashlib.sha256(f"{salt}:{case_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def audit_units(case_ids: np.ndarray, salt: str) -> np.ndarray:
    return np.array([_unit_interval(salt, str(case_id)) for case_id in case_ids])


def apply_policy(policy: Policy, batch: Batch, audit_u: np.ndarray | None = None) -> np.ndarray:
    n = len(batch)
    route = np.full(n, ACCEPT, dtype=int)
    if policy.family == "sample_audit":
        if audit_u is None:
            audit_u = audit_units(batch.case_id, policy.params["salt"])
        route[audit_u < float(policy.params["p"])] = AUDIT
        return route
    mandatory = _mandatory(policy, batch)
    if policy.family == "two_key":
        route[mandatory] = DUAL
    elif policy.family == "state_check":
        route[mandatory] = REVIEW
    tau = policy.params.get("tau")
    if tau is not None and float(tau) >= 0:
        extra = (~mandatory) & (batch.confidence <= float(tau))
        route[extra] = REVIEW
    return route


def _mandatory(policy: Policy, batch: Batch) -> np.ndarray:
    if policy.family == "two_key":
        categories = policy.params.get("categories") or []
        mask = np.isin(batch.category, list(categories)) if categories else np.zeros(len(batch), dtype=bool)
        cutoff = policy.params.get("amount_cutoff")
        if cutoff is not None:
            mask = mask | (batch.amount >= float(cutoff))
        return mask
    if policy.family == "state_check":
        return batch.violation.astype(bool)
    return np.zeros(len(batch), dtype=bool)


def _metrics(
    route: np.ndarray,
    batch: Batch,
    cost: CostModel,
    n_period: int,
    alpha: float,
    human_accuracy: float,
) -> dict:
    counts = count_routes(route, batch.correct, batch.exception)
    successes = counts["n_success_if_perfect_human"] if human_accuracy >= 1.0 - 1e-15 else None
    # For a_h < 1 the Bernoulli successes are only the accepted correct cases;
    # reliability_bound applies the assumption to the rest.
    if human_accuracy >= 1.0 - 1e-15:
        point, lcb, wilson = reliability_bound(
            counts["n_success_if_perfect_human"],
            len(batch),
            accepted_successes=counts["n_accepted_success"],
            n_accepted=counts["n_accepted"],
            human_accuracy=1.0,
            alpha=alpha,
        )
    else:
        point, lcb, wilson = reliability_bound(
            0,
            len(batch),
            accepted_successes=counts["n_accepted_success"],
            n_accepted=counts["n_accepted"],
            human_accuracy=human_accuracy,
            alpha=alpha,
        )
    work = workload_from_counts(
        n_sample=len(batch),
        n_review=counts["n_review"],
        n_exception=counts["n_exception"],
        n_dual=counts["n_dual"],
        n_audit=counts["n_audit"],
        n_accept=counts["n_accept"],
        n_miss=counts["n_miss"],
        model_dollars_sample=float(batch.model_cost.sum()),
        cost=cost,
        n_period=n_period,
    )
    counts["point"] = point
    counts["lcb"] = lcb
    counts["wilson"] = wilson
    counts["hours"] = work.hours
    counts["dollars"] = work.dollars
    counts["baseline_hours"] = work.baseline_hours
    counts["baseline_dollars"] = work.baseline_dollars
    counts["breakeven_cases"] = work.breakeven_cases
    counts["model_dollars"] = work.model_dollars
    counts["selection_dollars"] = _selection_dollars(
        work.dollars,
        counts["n_miss"],
        len(batch),
        n_period,
        cost,
        alpha,
    )
    return counts


def search_policies(
    batch: Batch,
    cost: CostModel,
    *,
    target: float,
    alpha: float,
    human_accuracy: float,
    n_period: int,
    salt: str,
    category_sets: tuple[tuple[str, ...], ...] = DEFAULT_CATEGORY_SETS,
    amount_cutoffs: tuple[float | None, ...] = DEFAULT_AMOUNT_CUTOFFS,
) -> list[Policy]:
    """Every candidate the calibration split can see, with calibration metrics attached."""
    policies: list[Policy] = []
    policies.extend(
        _threshold_sweep(
            batch,
            mandatory=np.zeros(len(batch), dtype=bool),
            family="threshold",
            label_prefix="Confidence threshold",
            extra_params={},
            cost=cost,
            alpha=alpha,
            human_accuracy=human_accuracy,
            n_period=n_period,
        )
    )
    for categories in category_sets:
        for cutoff in amount_cutoffs:
            mandatory = np.isin(batch.category, list(categories))
            if cutoff is not None:
                mandatory = mandatory | (batch.amount >= cutoff)
            if not mandatory.any():
                continue
            policies.extend(
                _threshold_sweep(
                    batch,
                    mandatory=mandatory,
                    family="two_key",
                    label_prefix="Two-key "
                    + "+".join(categories)
                    + ("" if cutoff is None else f", amount ≥ {cutoff:.0f}"),
                    extra_params={"categories": list(categories), "amount_cutoff": cutoff},
                    cost=cost,
                    alpha=alpha,
                    human_accuracy=human_accuracy,
                    n_period=n_period,
                )
            )
    if batch.violation.any():
        policies.extend(
            _threshold_sweep(
                batch,
                mandatory=batch.violation.astype(bool),
                family="state_check",
                label_prefix="State check",
                extra_params={},
                cost=cost,
                alpha=alpha,
                human_accuracy=human_accuracy,
                n_period=n_period,
            )
        )
    policies.extend(
        _audit_sweep(
            batch,
            salt=salt,
            cost=cost,
            alpha=alpha,
            human_accuracy=human_accuracy,
            n_period=n_period,
        )
    )
    _mark_references(policies, batch)
    # ``target`` is applied by the caller. Search itself does not drop policies:
    # the holdout report needs the dominated ones that illustrate the trap.
    del target
    return policies


def _threshold_sweep(
    batch: Batch,
    *,
    mandatory: np.ndarray,
    family: str,
    label_prefix: str,
    extra_params: dict,
    cost: CostModel,
    alpha: float,
    human_accuracy: float,
    n_period: int,
) -> list[Policy]:
    optional = np.flatnonzero(~mandatory)
    order = optional[np.argsort(batch.confidence[optional], kind="mergesort")]
    confs = batch.confidence[order]
    if len(order) == 0:
        boundaries = np.array([0], dtype=int)
    else:
        if len(confs) == 1:
            cuts = np.array([1], dtype=int)
        else:
            cuts = np.flatnonzero(np.diff(confs)) + 1
        boundaries = np.unique(np.concatenate([np.array([0], dtype=int), cuts, [len(order)]]))
    wrong = (~batch.correct[order]).astype(int) if len(order) else np.array([], dtype=int)
    exc = batch.exception[order].astype(int) if len(order) else np.array([], dtype=int)
    wrong_prefix = np.cumsum(wrong) if len(wrong) else np.array([], dtype=int)
    exc_prefix = np.cumsum(exc) if len(exc) else np.array([], dtype=int)
    total_wrong = int(wrong.sum()) if len(wrong) else 0
    n_mand = int(mandatory.sum())
    mand_exception = int((mandatory & batch.exception).sum())
    out: list[Policy] = []
    for k in boundaries.tolist():
        k = int(k)
        extra_exc = int(exc_prefix[k - 1]) if k else 0
        extra_review = k - extra_exc
        misses = total_wrong - (int(wrong_prefix[k - 1]) if k else 0)
        if family == "two_key":
            n_dual = n_mand
            n_exception = extra_exc
            n_review = extra_review
        elif family == "state_check":
            n_dual = 0
            n_exception = extra_exc + mand_exception
            n_review = extra_review + (n_mand - mand_exception)
        else:
            n_dual = 0
            n_exception = extra_exc
            n_review = extra_review
        n_accept = len(batch) - n_dual - n_exception - n_review
        n_miss = int(misses)
        tau = -1.0 if k == 0 else float(confs[k - 1])
        counts = _metrics_from_parts(
            n=len(batch),
            n_review=n_review,
            n_exception=n_exception,
            n_dual=n_dual,
            n_audit=0,
            n_accept=n_accept,
            n_miss=n_miss,
            model_cost=float(batch.model_cost.sum()),
            cost=cost,
            n_period=n_period,
            alpha=alpha,
            human_accuracy=human_accuracy,
        )
        family_name = "autonomous" if family == "threshold" and tau < 0 else family
        if family_name == "autonomous":
            label = "Autonomous"
            policy_id = "autonomous"
        else:
            label = f"{label_prefix} τ≤{tau:.4f}" if tau >= 0 else label_prefix
            policy_id = f"{family}:{_param_token(extra_params)}:tau={tau:.6f}"
        policy = Policy(
            policy_id=policy_id,
            family=family_name,
            label=label,
            params={**extra_params, "tau": None if tau < 0 and family_name == "autonomous" else tau},
            dev_lcb=float(counts["lcb"]),
            dev_point=float(counts["point"]),
            dev_wilson=float(counts["wilson"]),
            dev_dollars=float(counts["dollars"]),
            selection_dollars=float(counts["selection_dollars"]),
            dev_hours=float(counts["hours"]),
            dev_counts=counts,
        )
        out.append(policy)
    return out


def _audit_sweep(batch, *, salt, cost, alpha, human_accuracy, n_period) -> list[Policy]:
    units = audit_units(batch.case_id, salt)
    out = []
    for p in np.round(np.linspace(0.0, 1.0, 21), 2):
        policy = Policy(
            policy_id=f"sample_audit:p={p:.2f}",
            family="sample_audit",
            label=f"Sample audit {p:.0%}",
            params={"p": float(p), "salt": salt},
        )
        route = apply_policy(policy, batch, units)
        counts = _metrics(route, batch, cost, n_period, alpha, human_accuracy)
        policy.dev_lcb = float(counts["lcb"])
        policy.dev_point = float(counts["point"])
        policy.dev_wilson = float(counts["wilson"])
        policy.dev_dollars = float(counts["dollars"])
        policy.selection_dollars = float(counts["selection_dollars"])
        policy.dev_hours = float(counts["hours"])
        policy.dev_counts = counts
        out.append(policy)
    return out


def _metrics_from_parts(
    *,
    n,
    n_review,
    n_exception,
    n_dual,
    n_audit,
    n_accept,
    n_miss,
    model_cost,
    cost,
    n_period,
    alpha,
    human_accuracy,
) -> dict:
    n_accepted_success = n_accept - n_miss
    if human_accuracy >= 1.0 - 1e-15:
        successes = n - n_miss
        point, lcb, wilson = reliability_bound(
            successes,
            n,
            accepted_successes=n_accepted_success,
            n_accepted=n_accept,
            human_accuracy=1.0,
            alpha=alpha,
        )
    else:
        point, lcb, wilson = reliability_bound(
            0,
            n,
            accepted_successes=n_accepted_success,
            n_accepted=n_accept,
            human_accuracy=human_accuracy,
            alpha=alpha,
        )
    work = workload_from_counts(
        n_sample=n,
        n_review=n_review,
        n_exception=n_exception,
        n_dual=n_dual,
        n_audit=n_audit,
        n_accept=n_accept,
        n_miss=n_miss,
        model_dollars_sample=model_cost,
        cost=cost,
        n_period=n_period,
    )
    return {
        "n_review": n_review,
        "n_exception": n_exception,
        "n_dual": n_dual,
        "n_audit": n_audit,
        "n_accept": n_accept,
        "n_miss": n_miss,
        "n_accepted": n_accept,
        "n_accepted_success": n_accepted_success,
        "n_success_if_perfect_human": n - n_miss if human_accuracy >= 1 - 1e-15 else n_accepted_success,
        "point": point,
        "lcb": lcb,
        "wilson": wilson,
        "hours": work.hours,
        "dollars": work.dollars,
        "baseline_hours": work.baseline_hours,
        "baseline_dollars": work.baseline_dollars,
        "breakeven_cases": work.breakeven_cases,
        "model_dollars": work.model_dollars,
        "selection_dollars": _selection_dollars(
            work.dollars, n_miss, n, n_period, cost, alpha
        ),
    }


def _selection_dollars(point_dollars, n_miss, n, n_period, cost, alpha) -> float:
    """Point cost, plus the priced gap between observed misses and their upper bound.

    A calibration sample can look cheap because it happened to contain few shipped
    errors. Ranking on that point cost under-reviews relative to the true optimum
    when a shipped error is much more expensive than a review. Charging the
    one-sided upper bound on the miss rate is the same caution the reliability
    gate already applies, pointed at the cost.
    """
    if n <= 0:
        return float(point_dollars)
    upper = clopper_pearson_upper(int(n_miss), int(n), alpha)
    extra_misses = max(0.0, upper * n - n_miss)
    extra_hours = extra_misses * (n_period / n) * cost.hours_per_correction
    return float(point_dollars + extra_hours * cost.hourly_rate)


def _param_token(params: dict) -> str:
    cats = ",".join(params.get("categories") or []) or "-"
    cutoff = params.get("amount_cutoff")
    cutoff_s = "-" if cutoff is None else f"{float(cutoff):.0f}"
    if "categories" not in params and "amount_cutoff" not in params:
        return "-"
    return f"{cats}|{cutoff_s}"


def _mark_references(policies: list[Policy], batch: Batch) -> None:
    threshold = [p for p in policies if p.family in {"threshold", "autonomous"}]
    if not threshold:
        return
    # Naive plan: human touch as close as possible to 10% of the calibration book,
    # using confidence only. This is the "automate ~90%" proposal.
    target_touch = 0.10 * len(batch)
    def touch(policy: Policy) -> float:
        counts = policy.dev_counts
        return counts["n_review"] + counts["n_exception"] + counts["n_dual"] + counts["n_audit"]

    naive = min(threshold, key=lambda policy: (abs(touch(policy) - target_touch), policy.dev_dollars))
    naive.reference = "naive_90"
    for policy in policies:
        if policy.family == "autonomous":
            policy.reference = "autonomous"


def evaluate_policy(
    policy: Policy,
    batch: Batch,
    cost: CostModel,
    *,
    n_period: int,
    alpha: float,
    human_accuracy: float,
    audit_u: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    route = apply_policy(policy, batch, audit_u)
    return route, _metrics(route, batch, cost, n_period, alpha, human_accuracy)


def pareto(policies: list[Policy]) -> list[Policy]:
    """Non-dominated policies on calibration cost (lower) and calibration LCB (higher)."""
    ordered = sorted(policies, key=lambda policy: (policy.dev_dollars, -policy.dev_lcb, policy.policy_id))
    best_lcb = -1.0
    kept: list[Policy] = []
    seen: set[str] = set()
    for policy in ordered:
        if policy.dev_lcb > best_lcb + 1e-12:
            kept.append(policy)
            best_lcb = policy.dev_lcb
            seen.add(policy.policy_id)
    for policy in policies:
        if policy.reference and policy.policy_id not in seen:
            kept.append(policy)
            seen.add(policy.policy_id)
    return kept


def select_policy(policies: list[Policy], target: float) -> tuple[Policy | None, str]:
    """Cheapest calibration policy whose calibration lower bound clears ``target``.

    Returns ``(policy, reason)``. ``reason`` is ``selected`` or ``none``.
    The caller must not replace this choice after seeing holdout outcomes.
    """
    eligible = [
        policy
        for policy in policies
        if np.isfinite(policy.dev_lcb) and policy.dev_lcb >= target
    ]
    if not eligible:
        return None, "none"
    chosen = min(
        eligible,
        key=lambda policy: (policy.selection_dollars, -policy.dev_lcb, policy.policy_id),
    )
    return chosen, "selected"
