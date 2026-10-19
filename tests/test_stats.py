"""Bounds against closed forms, and a coverage check that can fail the wrong way."""

import numpy as np
import pytest
from scipy.stats import norm

from breakeven.evaluate import binomial_coverage, end_to_end_coverage, oracle_threshold
from breakeven.stats import (
    clopper_pearson_lower,
    clopper_pearson_upper,
    minimum_human_accuracy,
    reliability_bound,
    wilson_lower,
)


def test_clopper_pearson_all_successes_matches_power():
    # Beta(n, 1) has CDF x^n, so the alpha quantile is alpha ** (1/n).
    n, alpha = 20, 0.05
    assert clopper_pearson_lower(n, n, alpha) == pytest.approx(alpha ** (1 / n))


def test_clopper_pearson_no_successes_is_zero():
    assert clopper_pearson_lower(0, 50, 0.05) == 0.0


def test_clopper_pearson_upper_no_events_matches_power():
    # P(X = 0 | p) = (1-p)^n = alpha ⇒ p = 1 - alpha ** (1/n).
    n, alpha = 20, 0.05
    assert clopper_pearson_upper(0, n, alpha) == pytest.approx(1 - alpha ** (1 / n))


def test_wilson_matches_algebra():
    k, n, alpha = 90, 100, 0.05
    z = float(norm.ppf(1 - alpha))
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = p + z2 / (2 * n)
    margin = z * (p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5
    expected = (center - margin) / denom
    assert wilson_lower(k, n, alpha) == pytest.approx(expected)


def test_minimum_human_accuracy_closed_form():
    n_acc, n, alpha, target = 40, 100, 0.05, 0.95
    lower = clopper_pearson_lower(40, n_acc, alpha)
    expected = (target * n - n_acc * lower) / (n - n_acc)
    assert minimum_human_accuracy(40, n_acc, n, target, alpha) == pytest.approx(expected)


def test_perfect_review_bound_is_the_binomial_one():
    point, lcb, _wilson = reliability_bound(
        95, 100, accepted_successes=45, n_accepted=50, human_accuracy=1.0, alpha=0.05
    )
    assert point == pytest.approx(0.95)
    assert lcb == pytest.approx(clopper_pearson_lower(95, 100, 0.05))


def test_wilson_can_undercover_where_clopper_pearson_does_not():
    rows = binomial_coverage(reps=4000, ns=(20,), ps=(0.80,), seed=0, alpha=0.05)
    row = rows[0]
    assert row["clopper_pearson_coverage"] >= 0.97
    assert row["wilson_coverage"] < 0.96
    assert row["wilson_coverage"] < row["clopper_pearson_coverage"]


def test_end_to_end_bound_covers_and_oracle_tau_is_the_error_rate():
    assert oracle_threshold(0.18, 0.95, 0.25, 1.60) == pytest.approx(0.18)
    result = end_to_end_coverage(reps=200, n=500, seed=3)
    assert result["reported"] == 200
    assert result["coverage"] >= 0.95
    assert abs(result["mean_tau_gap"]) < 0.05


def test_vectorized_lower_bound_matches_scalar():
    ks = np.array([0, 1, 10, 20])
    from breakeven.stats import clopper_pearson_lower_array

    got = clopper_pearson_lower_array(ks, 20, 0.05)
    for k, value in zip(ks, got, strict=True):
        assert value == pytest.approx(clopper_pearson_lower(int(k), 20, 0.05))
