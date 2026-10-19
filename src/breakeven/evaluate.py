"""Honest checks on the bound and on the policy search.

1. Coverage of the reported lower bound on iid Bernoulli trials, over repeated
   draws. Clopper–Pearson should land at or above the nominal level. Wilson is
   measured the same way and is not assumed to.
2. End-to-end coverage: pick a threshold on a calibration draw, freeze it, and
   ask whether the holdout lower bound sits under the policy's true reliability.
3. Gap to an oracle that knows the reliability of every candidate and picks the
   cheapest one that truly clears the target.
"""

from __future__ import annotations

import math

import numpy as np

from breakeven.stats import clopper_pearson_lower_array, clopper_pearson_upper, wilson_lower


def binomial_coverage(
    *,
    reps: int = 1000,
    alpha: float = 0.05,
    ns: tuple[int, ...] = (20, 40, 200, 800),
    ps: tuple[float, ...] = (0.70, 0.80, 0.90, 0.95),
    seed: int = 7,
) -> list[dict]:
    rng = np.random.default_rng(seed)
    rows = []
    for n in ns:
        for p in ps:
            draws = rng.binomial(n, p, size=reps)
            cp = clopper_pearson_lower_array(draws, n, alpha)
            wil = np.asarray(wilson_lower(draws, n, alpha), dtype=float)
            rows.append(
                {
                    "n": n,
                    "p": p,
                    "reps": reps,
                    "alpha": alpha,
                    "clopper_pearson_coverage": float(np.mean(cp <= p + 1e-12)),
                    "wilson_coverage": float(np.mean(wil <= p + 1e-12)),
                    "clopper_pearson_mean_gap": float(np.mean(p - cp)),
                    "wilson_mean_gap": float(np.mean(p - wil)),
                }
            )
    return rows


def _analytical_reliability(tau: float, err_rate: float, human_accuracy: float = 1.0) -> float:
    """Confidence and latent difficulty are the same Uniform(0, 1) draw.

    The case is wrong when u < err_rate. Review when u <= tau. Reviewed cases
    contribute ``human_accuracy``; accepted mistakes contribute 0; accepted
    correct cases contribute 1.
    """
    tau = float(np.clip(tau, 0.0, 1.0))
    miss = max(err_rate - tau, 0.0)
    reviewed = tau
    accepted_correct = max(1.0 - max(err_rate, tau), 0.0)
    return reviewed * human_accuracy + accepted_correct


def _analytical_variable_hours(tau: float, err_rate: float, t_review: float, t_correction: float) -> float:
    tau = float(np.clip(tau, 0.0, 1.0))
    miss = max(err_rate - tau, 0.0)
    return tau * t_review + miss * t_correction


def oracle_threshold(err_rate: float, target: float, t_review: float, t_correction: float) -> float:
    """Cheapest tau in [0, 1] whose true reliability meets ``target`` (perfect reviewer)."""
    # Reliability increases with tau. The minimum tau that hits the target:
    # R = 1 - max(err_rate - tau, 0) >= target  =>  max(err_rate - tau, 0) <= 1 - target
    # => tau >= err_rate - (1 - target), and tau >= 0.
    slack = 1.0 - target
    min_tau = max(0.0, err_rate - slack)
    # Above min_tau, misses fall and reviews rise. If a miss costs more than a
    # review, the optimum is the smallest tau that catches every error, which
    # is err_rate, but only when that tau still... it always has R=1.
    # Compare cost at min_tau (some misses allowed) vs at err_rate (no misses)
    # vs anywhere above err_rate (extra reviews, no fewer misses).
    if t_correction <= t_review:
        return min_tau
    # Moving tau from min_tau up to err_rate replaces misses with reviews.
    # Each unit of tau removes one unit of misses and adds one unit of reviews.
    # Net variable-cost slope = t_review - t_correction < 0, so go to err_rate.
    # Above err_rate the slope is +t_review, so stop.
    return max(min_tau, err_rate)


def end_to_end_coverage(
    *,
    reps: int = 1000,
    n: int = 500,
    err_rate: float = 0.18,
    target: float = 0.95,
    alpha: float = 0.05,
    t_review: float = 0.25,
    t_correction: float = 1.60,
    seed: int = 11,
) -> dict:
    """Select a threshold on a calibration draw and bound it on a disjoint holdout.

    The true reliability is the analytical value for the frozen tau, so the
    check is not limited by a second Monte Carlo.
    """
    rng = np.random.default_rng(seed)
    covered = 0
    reported = 0
    gaps = []
    true_rs = []
    lcbs = []
    oracle = oracle_threshold(err_rate, target, t_review, t_correction)
    oracle_cost = _analytical_variable_hours(oracle, err_rate, t_review, t_correction)
    cost_gaps = []
    cost_gaps_abs = []
    for _ in range(reps):
        u = rng.random(n)
        correct = u >= err_rate
        perm = rng.permutation(n)
        cut = int(round(0.6 * n))
        dev = perm[:cut]
        hold = perm[cut:]
        chosen = _select_tau(u[dev], correct[dev], target, alpha, t_review, t_correction)
        if chosen is None:
            continue
        reported += 1
        hold_u = u[hold]
        hold_correct = correct[hold]
        reviewed = hold_u <= chosen
        successes = int((reviewed | hold_correct).sum())
        lcb = float(clopper_pearson_lower_array(np.array([successes]), len(hold), alpha)[0])
        truth = _analytical_reliability(chosen, err_rate)
        true_rs.append(truth)
        lcbs.append(lcb)
        covered += int(lcb <= truth + 1e-9)
        cost = _analytical_variable_hours(chosen, err_rate, t_review, t_correction)
        gaps.append(chosen - oracle)
        cost_gaps_abs.append(cost - oracle_cost)
        if oracle_cost > 0:
            cost_gaps.append((cost - oracle_cost) / oracle_cost)
        else:
            cost_gaps.append(0.0 if math.isclose(cost, oracle_cost) else float("inf"))
    return {
        "reps": reps,
        "reported": reported,
        "n": n,
        "err_rate": err_rate,
        "target": target,
        "alpha": alpha,
        "coverage": (covered / reported) if reported else float("nan"),
        "mean_lcb": float(np.mean(lcbs)) if lcbs else float("nan"),
        "mean_true_reliability": float(np.mean(true_rs)) if true_rs else float("nan"),
        "oracle_tau": oracle,
        "mean_tau_gap": float(np.mean(gaps)) if gaps else float("nan"),
        "mean_relative_variable_cost_gap": float(np.mean(cost_gaps)) if cost_gaps else float("nan"),
        "mean_variable_hours_gap": float(np.mean(cost_gaps_abs)) if cost_gaps_abs else float("nan"),
    }


def _select_tau(
    confidence: np.ndarray,
    correct: np.ndarray,
    target: float,
    alpha: float,
    t_review: float,
    t_correction: float,
) -> float | None:
    order = np.argsort(confidence, kind="mergesort")
    conf = confidence[order]
    wrong = (~correct[order]).astype(int)
    if len(conf) == 1:
        cuts = np.array([1])
    else:
        cuts = np.flatnonzero(np.diff(conf)) + 1
    ks = np.unique(np.concatenate([[0], cuts, [len(conf)]]))
    wrong_prefix = np.cumsum(wrong)
    total_wrong = int(wrong.sum())
    n = len(conf)
    best_cost = None
    best_tau = None
    for k in ks.tolist():
        misses = total_wrong - (int(wrong_prefix[k - 1]) if k else 0)
        successes = n - misses
        lcb = float(clopper_pearson_lower_array(np.array([successes]), n, alpha)[0])
        if lcb < target:
            continue
        tau = 0.0 if k == 0 else float(conf[k - 1])
        # Cost on this calibration sample, in hours, maintenance omitted (constant).
        # Same conservative miss price as policy search: charge the upper bound.
        upper = clopper_pearson_upper(int(misses), n, alpha)
        cost = k * t_review + upper * n * t_correction
        if best_cost is None or cost < best_cost - 1e-12 or (
            math.isclose(cost, best_cost) and (best_tau is None or tau < best_tau)
        ):
            best_cost = cost
            best_tau = tau
    return best_tau


def run_evaluation(*, reps: int = 1000, e2e_reps: int = 1000, seed: int = 7) -> dict:
    # The binomial table is cheap, so it uses at least 10,000 draws. A 1,000-draw
    # estimate of a 95% coverage has a standard error near 0.7 points, enough to
    # make a valid bound look like it undercoveres.
    binomial = binomial_coverage(reps=max(reps, 10_000), seed=seed)
    e2e = end_to_end_coverage(reps=e2e_reps, seed=seed + 1)
    return {
        "reps": reps,
        "binomial": binomial,
        "end_to_end": e2e,
        "llm_runs": None,
        "note": (
            "No live model was called. Coverage is a property of the bound and "
            "the select-then-freeze procedure on a known Bernoulli process."
        ),
    }
