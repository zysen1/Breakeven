"""One-sided binomial lower bounds.

The qualification gate is the Clopper–Pearson lower endpoint: the alpha
quantile of Beta(k, n - k + 1). It is exact for iid Bernoulli trials and
guarantees coverage of at least ``1 - alpha`` (it is conservative). Wilson's
score interval is reported beside it because it sits closer to the nominal
level, including occasionally below it. A go-live gate that undercoveres can
clear a system the evidence does not support, so Wilson is not the gate.

When escalated cases are assumed to succeed with a known probability ``a_h``
rather than an observed outcome, the bound follows the READY appendix
construction: Clopper–Pearson is attached only to the accepted cases, and the
assumed review term is added with no statistical confidence.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.stats import beta, norm


def clopper_pearson_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper–Pearson lower bound at confidence ``1 - alpha``."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"invalid binomial counts k={k} n={n}")
    if n == 0:
        return float("nan")
    if k == 0:
        return 0.0
    return float(beta.ppf(alpha, k, n - k + 1))


def clopper_pearson_upper(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper–Pearson upper bound at confidence ``1 - alpha``."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"invalid binomial counts k={k} n={n}")
    if n == 0:
        return float("nan")
    if k == n:
        return 1.0
    return float(beta.ppf(1.0 - alpha, k + 1, n - k))


def clopper_pearson_lower_array(k: np.ndarray, n: int, alpha: float = 0.05) -> np.ndarray:
    """Vectorized lower bound. ``n`` is shared; ``k`` is an integer array."""
    k = np.asarray(k, dtype=int)
    out = np.zeros(k.shape, dtype=float)
    positive = k > 0
    if np.any(positive):
        kk = k[positive]
        out[positive] = beta.ppf(alpha, kk, n - kk + 1)
    return out


def wilson_lower(k: int | np.ndarray, n: int, alpha: float = 0.05) -> float | np.ndarray:
    """One-sided Wilson score lower bound.

    ``(center - z * sqrt(p(1-p)/n + z^2/(4n^2))) / (1 + z^2/n)`` with
    ``z = Phi^{-1}(1 - alpha)``.
    """
    if n <= 0:
        return float("nan")
    z = float(norm.ppf(1.0 - alpha))
    scalar = np.isscalar(k)
    kk = np.asarray(k, dtype=float)
    p = kk / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * np.sqrt(np.maximum(p * (1.0 - p) / n + z2 / (4.0 * n * n), 0.0))
    lower = np.clip((center - margin) / denom, 0.0, 1.0)
    if scalar:
        return float(lower)
    return lower


def reliability_bound(
    successes: int,
    n: int,
    *,
    accepted_successes: int,
    n_accepted: int,
    human_accuracy: float,
    alpha: float,
) -> tuple[float, float, float]:
    """Return ``(point, clopper_pearson_lcb, wilson_lcb)``.

    If ``human_accuracy == 1``, escalations are observed successes and both
    bounds are ordinary one-sided intervals on ``successes / n``.

    If ``human_accuracy < 1``, only accepted cases are Bernoulli. The lower
    bound is

        (n_accepted / n) * L(accepted) + ((n - n_accepted) / n) * a_h

    which is conditional on the observed routing mix. The point estimate uses
    the same weights.
    """
    if n <= 0:
        return float("nan"), float("nan"), float("nan")
    if human_accuracy >= 1.0 - 1e-15:
        point = successes / n
        return (
            point,
            clopper_pearson_lower(successes, n, alpha),
            float(wilson_lower(successes, n, alpha)),
        )
    n_other = n - n_accepted
    if n_accepted == 0:
        point = float(human_accuracy)
        return point, point, point
    p_acc = accepted_successes / n_accepted
    point = (n_accepted / n) * p_acc + (n_other / n) * human_accuracy
    lcb_acc = clopper_pearson_lower(accepted_successes, n_accepted, alpha)
    wil_acc = float(wilson_lower(accepted_successes, n_accepted, alpha))
    weight_acc = n_accepted / n
    weight_other = n_other / n
    return (
        point,
        weight_acc * lcb_acc + weight_other * human_accuracy,
        weight_acc * wil_acc + weight_other * human_accuracy,
    )


def minimum_human_accuracy(
    accepted_successes: int,
    n_accepted: int,
    n: int,
    target: float,
    alpha: float,
) -> float | None:
    """Smallest constant review accuracy at which the conditional LCB still clears ``target``.

    Returns ``None`` when no accuracy in ``[0, 1]`` qualifies. Returns ``0`` when the
    accepted-case bound alone already clears the target (the claim does not depend on
    the reviewer). Mirrors the break-even review assumption in READY.
    """
    if n <= 0 or not math.isfinite(target):
        return None
    n_other = n - n_accepted
    if n_accepted == 0:
        return 0.0 if target <= 0 else (target if target <= 1 else None)
    lcb_acc = clopper_pearson_lower(accepted_successes, n_accepted, alpha)
    # (n_acc/n)*L + (n_other/n)*a >= target
    # a >= (target - weight_acc*L) / weight_other
    if n_other == 0:
        return 0.0 if lcb_acc >= target else None
    needed = (target * n - n_accepted * lcb_acc) / n_other
    if needed <= 0:
        return 0.0
    if needed > 1:
        return None
    return float(needed)
