"""End-to-end shadow study: split, search, qualify, project costs.

Policy parameters are chosen on the calibration split. The holdout split is
used once, for the lower bound and for the operating-cost estimate of that
frozen policy. Per-category accuracy on the calibration split is a diagnostic
for the customer; it is not an automatic search over categories.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from breakeven.agents import build_mock
from breakeven.assertions import load_assertions, violation_mask, violation_summary
from breakeven.cost import curve, tornado
from breakeven.data import generate_cases, read_cases, write_cases
from breakeven.models import Case, CostModel, Prediction, Target
from breakeven.policies import (
    Batch,
    Policy,
    audit_units,
    evaluate_policy,
    search_policies,
    select_policy,
)
from breakeven.runner import run_shadow
from breakeven.stats import minimum_human_accuracy, reliability_bound

ACME_COST = CostModel(
    currency="USD",
    hourly_rate=55.0,
    hours_per_case_human=0.20,
    hours_per_review=0.25,
    hours_per_exception=0.50,
    hours_per_correction=1.60,
    hours_per_audit=0.20,
    hours_per_dual_review=0.50,
    maintenance_hours=16.0,
    human_maintenance_hours=0.0,
    model_cost_per_case=0.0,
    human_accuracy=1.0,
)

AGENT_ORDER = ("brash", "careful", "health_blind", "scattered")

AGENT_COPY = {
    "brash": {
        "name": "Brash",
        "summary": "Higher accuracy. Confidence does not know when it is wrong.",
    },
    "careful": {
        "name": "Careful",
        "summary": "Lower accuracy. Confidence ranks the cases it gets wrong.",
    },
    "health_blind": {
        "name": "Health-blind",
        "summary": "Same accuracy as Careful. Health claims fail at high confidence.",
    },
    "scattered": {
        "name": "Scattered",
        "summary": "Careful's accuracy, with confidence unrelated to correctness.",
    },
}


def split_indices(cases: list[Case], fraction: float, seed: int) -> tuple[list[int], list[int]]:
    rng = np.random.default_rng(seed)
    buckets: dict[str, list[int]] = {}
    for index, case in enumerate(cases):
        buckets.setdefault(case.category, []).append(index)
    dev: list[int] = []
    hold: list[int] = []
    for category in sorted(buckets):
        idx = np.array(buckets[category], dtype=int)
        rng.shuffle(idx)
        cut = int(round(len(idx) * fraction))
        if len(idx) >= 2:
            cut = min(max(cut, 1), len(idx) - 1)
        dev.extend(idx[:cut].tolist())
        hold.extend(idx[cut:].tolist())
    return dev, hold


def build_batch(cases: list[Case], predictions: list[Prediction], violations: np.ndarray) -> Batch:
    by_id = {prediction.case_id: prediction for prediction in predictions}
    return Batch(
        case_id=np.array([case.case_id for case in cases]),
        category=np.array([case.category for case in cases]),
        amount=np.array([case.amount for case in cases], dtype=float),
        exception=np.array([case.exception_flag for case in cases], dtype=bool),
        correct=np.array(
            [by_id[case.case_id].decision == case.decision_truth for case in cases],
            dtype=bool,
        ),
        confidence=np.array([by_id[case.case_id].confidence for case in cases], dtype=float),
        violation=np.asarray(violations, dtype=bool),
        model_cost=np.array([by_id[case.case_id].model_cost for case in cases], dtype=float),
    )


def _slice_batch(batch: Batch, indices: list[int]) -> Batch:
    idx = np.array(indices, dtype=int)
    return Batch(
        case_id=batch.case_id[idx],
        category=batch.category[idx],
        amount=batch.amount[idx],
        exception=batch.exception[idx],
        correct=batch.correct[idx],
        confidence=batch.confidence[idx],
        violation=batch.violation[idx],
        model_cost=batch.model_cost[idx],
    )


def _records(cases: list[Case], predictions: list[Prediction]) -> list[dict]:
    by_id = {prediction.case_id: prediction for prediction in predictions}
    rows = []
    for case in cases:
        prediction = by_id[case.case_id]
        rows.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "amount": case.amount,
                "state": case.state,
                "documents_complete": case.documents_complete,
                "fraud_signal": case.fraud_signal,
                "exception_flag": case.exception_flag,
                "region": case.region,
                "prior_claims": case.prior_claims,
                "claimant_tenure_years": case.claimant_tenure_years,
                "action": prediction.action,
                "decision": prediction.decision,
                "confidence": prediction.confidence,
            }
        )
    return rows


def _rates(counts: dict, n: int) -> dict[str, float]:
    handled = counts["n_review"] + counts["n_exception"] + counts["n_dual"] + counts["n_audit"]
    return {
        "review": counts["n_review"] / n,
        "exception": counts["n_exception"] / n,
        "dual": counts["n_dual"] / n,
        "audit": counts["n_audit"] / n,
        "miss": counts["n_miss"] / n,
        "handled": handled / n,
    }


def _jsonable_policy(
    policy: Policy,
    hold_counts: dict,
    *,
    cost: CostModel,
    n_period: int,
    alpha: float,
    volumes: np.ndarray,
    assertion_stats: dict,
    bootstrap: dict | None,
) -> dict:
    n = hold_counts["n_review"] + hold_counts["n_exception"] + hold_counts["n_dual"]
    n += hold_counts["n_audit"] + hold_counts["n_accept"]
    rates = _rates(hold_counts, n)
    touch = 1.0 - (hold_counts["n_accept"] / n if n else 0.0)
    a_h_min = minimum_human_accuracy(
        int(hold_counts["n_accepted_success"]),
        int(hold_counts["n_accepted"]),
        n,
        # The minimum reviewer accuracy is a property of the frozen accept set.
        # The caller passes the deployment target via hold_counts if present.
        float(hold_counts.get("target", 0.0)),
        alpha,
    )
    return {
        "id": policy.policy_id,
        "family": policy.family,
        "label": policy.label,
        "params": policy.params,
        "reference": policy.reference,
        "dev_lcb": policy.dev_lcb,
        "dev_point": policy.dev_point,
        "dev_dollars": policy.dev_dollars,
        "selection_dollars": policy.selection_dollars,
        "holdout": {
            "n": n,
            "successes": int(hold_counts["n_success_if_perfect_human"]),
            "accepted": int(hold_counts["n_accepted"]),
            "accepted_successes": int(hold_counts["n_accepted_success"]),
            "reliability": hold_counts["point"],
            "lcb": hold_counts["lcb"],
            "wilson_lcb": hold_counts["wilson"],
            "automation_rate": hold_counts["n_accept"] / n if n else 0.0,
            "human_touch_rate": touch,
            "miss_rate": hold_counts["n_miss"] / n if n else 0.0,
            "hours": hold_counts["hours"],
            "dollars": hold_counts["dollars"],
            "baseline_hours": hold_counts["baseline_hours"],
            "baseline_dollars": hold_counts["baseline_dollars"],
            "breakeven_cases": hold_counts["breakeven_cases"],
            "model_dollars": hold_counts["model_dollars"],
            "savings_hours": hold_counts["baseline_hours"] - hold_counts["hours"],
            "savings_dollars": hold_counts["baseline_dollars"] - hold_counts["dollars"],
            "a_h_min": a_h_min,
            "bootstrap": bootstrap,
            "assertions": assertion_stats,
            "counts": {
                "review": hold_counts["n_review"],
                "exception": hold_counts["n_exception"],
                "dual": hold_counts["n_dual"],
                "audit": hold_counts["n_audit"],
                "accept": hold_counts["n_accept"],
                "miss": hold_counts["n_miss"],
            },
        },
        "curve": curve(rates, cost, volumes),
        "tornado": tornado(rates, cost, n_period),
        "lcb_by_human_accuracy": _lcb_grid(hold_counts, n, alpha),
    }


def _lcb_grid(counts: dict, n: int, alpha: float) -> dict[str, float]:
    out = {}
    for a_h in (1.0, 0.98, 0.95, 0.90):
        _point, lcb, _wilson = reliability_bound(
            int(counts["n_success_if_perfect_human"]) if a_h >= 1 else 0,
            n,
            accepted_successes=int(counts["n_accepted_success"]),
            n_accepted=int(counts["n_accepted"]),
            human_accuracy=a_h,
            alpha=alpha,
        )
        out[f"{a_h:.2f}"] = lcb
    return out


def _bootstrap_savings(
    route: np.ndarray,
    batch: Batch,
    cost: CostModel,
    n_period: int,
    *,
    n_boot: int,
    seed: int,
) -> dict[str, float]:
    """Percentiles of period dollar savings when the holdout rows are resampled."""
    if n_boot <= 0 or len(batch) == 0:
        return {}
    rng = np.random.default_rng(seed)
    per_hours = np.zeros(len(batch), dtype=float)
    review = (route == 1) & ~batch.exception
    exception = (route == 1) & batch.exception
    dual = route == 2
    audit = route == 3
    miss = (route == 0) & ~batch.correct
    per_hours[review] += cost.hours_per_review
    per_hours[exception] += cost.hours_per_exception
    per_hours[dual] += cost.hours_per_dual_review
    per_hours[audit] += cost.hours_per_audit
    per_hours[miss] += cost.hours_per_correction
    handled = review | exception | dual | audit
    per_hours[handled] += (1.0 - cost.human_accuracy) * cost.hours_per_correction
    n = len(batch)
    draws = rng.integers(0, n, size=(n_boot, n))
    variable = per_hours[draws].sum(axis=1) * (n_period / n)
    hours = variable + cost.maintenance_hours
    dollars = hours * cost.hourly_rate + n_period * cost.model_cost_per_case
    baseline = (n_period * cost.hours_per_case_human + cost.human_maintenance_hours) * cost.hourly_rate
    savings = baseline - dollars
    return {
        "p05": float(np.quantile(savings, 0.05)),
        "p50": float(np.quantile(savings, 0.50)),
        "p95": float(np.quantile(savings, 0.95)),
    }


def _category_rows(cases: list[Case], correct: np.ndarray, mask: np.ndarray | None = None) -> list[dict]:
    if mask is None:
        mask = np.ones(len(cases), dtype=bool)
    rows = []
    categories = sorted({case.category for case in cases})
    cat = np.array([case.category for case in cases])
    for name in categories:
        sel = mask & (cat == name)
        n = int(sel.sum())
        k = int(correct[sel].sum()) if n else 0
        rows.append({"category": name, "n": n, "correct": k, "accuracy": (k / n) if n else None})
    return rows


def qualify_agent(
    *,
    name: str,
    cases: list[Case],
    predictions: list[Prediction],
    dev_idx: list[int],
    hold_idx: list[int],
    violations: np.ndarray,
    cost: CostModel,
    target: Target,
    n_period: int,
    salt: str,
    n_boot: int,
) -> dict:
    batch = build_batch(cases, predictions, violations)
    dev = _slice_batch(batch, dev_idx)
    hold = _slice_batch(batch, hold_idx)
    policies = search_policies(
        dev,
        cost,
        target=target.reliability,
        alpha=target.alpha,
        human_accuracy=target.human_accuracy,
        n_period=n_period,
        salt=salt,
    )
    chosen, reason = select_policy(policies, target.reliability)
    # Export only the policies a target slider can actually select, plus the
    # reference policies (autonomous, naive ~90% plan). Selection uses the
    # calibration lower bound, so this set does not depend on the holdout.
    exported: list[Policy] = []
    seen: set[str] = set()
    for slider_target in np.round(np.arange(0.80, 1.00, 0.01), 2):
        slider_choice, _slider_reason = select_policy(policies, float(slider_target))
        if slider_choice is not None and slider_choice.policy_id not in seen:
            exported.append(slider_choice)
            seen.add(slider_choice.policy_id)
    for policy in policies:
        if policy.reference and policy.policy_id not in seen:
            exported.append(policy)
            seen.add(policy.policy_id)
    if chosen is not None and chosen.policy_id not in seen:
        exported.append(chosen)
    volumes = np.linspace(0, max(n_period * 4, 4000), 33)
    audit_u = audit_units(hold.case_id, salt)
    packed = []
    chosen_blob = None
    naive_blob = None
    for policy in exported:
        route, counts = evaluate_policy(
            policy,
            hold,
            cost,
            n_period=n_period,
            alpha=target.alpha,
            human_accuracy=target.human_accuracy,
            audit_u=audit_u,
        )
        counts = dict(counts)
        counts["target"] = target.reliability
        accepted = route == 0
        assertion_stats = violation_summary(hold.violation, accepted, target.alpha)
        boot = None
        if policy.reference == "naive_90" or (chosen is not None and policy.policy_id == chosen.policy_id):
            boot = _bootstrap_savings(
                route, hold, cost, n_period, n_boot=n_boot, seed=n_period + len(policy.policy_id)
            )
        blob = _jsonable_policy(
            policy,
            counts,
            cost=cost,
            n_period=n_period,
            alpha=target.alpha,
            volumes=volumes,
            assertion_stats=assertion_stats,
            bootstrap=boot,
        )
        packed.append(blob)
        if chosen is not None and policy.policy_id == chosen.policy_id:
            chosen_blob = blob
        if policy.reference == "naive_90":
            naive_blob = blob
    if chosen is None:
        # Still describe the calibration policy with the highest lower bound,
        # and mark that it was not eligible. Holdout numbers are descriptive.
        fallback = max(policies, key=lambda item: (item.dev_lcb, -item.dev_dollars))
        route, counts = evaluate_policy(
            fallback,
            hold,
            cost,
            n_period=n_period,
            alpha=target.alpha,
            human_accuracy=target.human_accuracy,
            audit_u=audit_u,
        )
        counts = dict(counts)
        counts["target"] = target.reliability
        chosen_blob = _jsonable_policy(
            fallback,
            counts,
            cost=cost,
            n_period=n_period,
            alpha=target.alpha,
            volumes=volumes,
            assertion_stats=violation_summary(hold.violation, route == 0, target.alpha),
            bootstrap=_bootstrap_savings(
                route, hold, cost, n_period, n_boot=n_boot, seed=11
            ),
        )
        status = "no_eligible_policy"
    else:
        status = _status(chosen_blob, target.reliability)
    accuracy = float(batch.correct.mean())
    dev_correct = batch.correct[np.array(dev_idx)]
    dev_cases = [cases[i] for i in dev_idx]
    return {
        "id": name,
        "name": AGENT_COPY.get(name, {}).get("name", name),
        "summary": AGENT_COPY.get(name, {}).get("summary", ""),
        "accuracy": accuracy,
        "n": len(cases),
        "n_correct": int(batch.correct.sum()),
        "status": status,
        "selection": reason if chosen is not None else "none",
        "selected_id": None if chosen_blob is None else chosen_blob["id"],
        "selected": chosen_blob,
        "naive": naive_blob,
        "policies": packed,
        "calibration_categories": _category_rows(dev_cases, dev_correct),
        "book_categories": _category_rows(cases, batch.correct),
        "agent_assertion_rate": float(batch.violation.mean()) if len(batch) else 0.0,
    }


def _status(blob: dict, target: float) -> str:
    hold = blob["holdout"]
    if not np.isfinite(hold["lcb"]) or hold["lcb"] < target:
        return "holdout_miss"
    if hold["savings_dollars"] <= 0:
        return "reliability_only"
    boot = hold.get("bootstrap") or {}
    if boot and boot.get("p05", 0.0) <= 0:
        return "cost_uncertain"
    return "go"


def run_predictions(
    cases: list[Case],
    agent_names: list[str],
    *,
    seed: int,
    cache_dir: Path,
    workers: int = 4,
) -> dict[str, list[Prediction]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in agent_names:
        agent = build_mock(name, seed)
        out[name] = run_shadow(
            cases,
            agent,
            cache_dir / f"{name}.jsonl",
            workers=workers,
        )
    return out


def run_qualification(
    cases: list[Case],
    predictions: dict[str, list[Prediction]],
    *,
    cost: CostModel,
    target: Target,
    assertions,
    seed: int,
    n_boot: int = 400,
) -> dict:
    dev_idx, hold_idx = split_indices(cases, 0.60, seed)
    n_period = len(cases)
    agents = []
    for name, preds in predictions.items():
        records = _records(cases, preds)
        violations = violation_mask(records, assertions) if assertions else np.zeros(len(cases), dtype=bool)
        agents.append(
            qualify_agent(
                name=name,
                cases=cases,
                predictions=preds,
                dev_idx=dev_idx,
                hold_idx=hold_idx,
                violations=violations,
                cost=cost,
                target=target,
                n_period=n_period,
                salt=f"{name}:{seed}",
                n_boot=n_boot,
            )
        )
    return {
        "n_cases": len(cases),
        "n_calibration": len(dev_idx),
        "n_holdout": len(hold_idx),
        "split_fraction": 0.60,
        "stratified": "category",
        "seed": seed,
        "target": target.model_dump(),
        "cost": cost.model_dump(),
        "agents": agents,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")


def load_or_generate_cases(path: Path, n: int, seed: int) -> list[Case]:
    if path.exists():
        return read_cases(path)
    cases = generate_cases(n, seed)
    write_cases(cases, path)
    return cases
