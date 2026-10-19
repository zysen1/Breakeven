"""Cost arithmetic and the calibration-set policy search."""

import numpy as np
import pytest

from breakeven.cost import breakeven_volume, workload_from_counts
from breakeven.models import CostModel
from breakeven.policies import Batch, evaluate_policy, search_policies, select_policy

COST = CostModel(
    hourly_rate=55,
    hours_per_case_human=0.20,
    hours_per_review=0.25,
    hours_per_exception=0.50,
    hours_per_correction=1.60,
    hours_per_audit=0.20,
    hours_per_dual_review=0.50,
    maintenance_hours=16,
    human_accuracy=1.0,
)


def test_breakeven_volume_closed_form():
    # N* = fixed / (human variable hours - agent variable hours), rate cancels.
    got = breakeven_volume(
        variable_hours_per_case=0.115,
        human_hours_per_case=0.20,
        maintenance_hours=16,
        hourly_rate=55,
        model_cost_per_case=0,
    )
    assert got == pytest.approx(16 / 0.085)


def test_breakeven_volume_is_never_when_variable_cost_is_worse():
    assert (
        breakeven_volume(
            variable_hours_per_case=0.30,
            human_hours_per_case=0.20,
            maintenance_hours=16,
            hourly_rate=55,
        )
        is None
    )


def test_workload_projection_matches_hand_total():
    work = workload_from_counts(
        n_sample=800,
        n_review=49,
        n_exception=27,
        n_dual=0,
        n_audit=0,
        n_accept=724,
        n_miss=103,
        model_dollars_sample=0,
        cost=COST,
        n_period=2000,
    )
    # 2.5 × (49×0.25 + 27×0.50 + 103×1.60) + 16
    assert work.hours == pytest.approx(2.5 * (49 * 0.25 + 27 * 0.50 + 103 * 1.60) + 16)
    assert work.baseline_hours == pytest.approx(400)


def test_search_reviews_the_errors_it_can_see():
    n = 200
    confidence = np.linspace(0.01, 0.99, n)
    correct = confidence > 0.20
    batch = Batch(
        case_id=np.array([f"c{i}" for i in range(n)]),
        category=np.array(["auto"] * n),
        amount=np.full(n, 100.0),
        exception=np.zeros(n, dtype=bool),
        correct=correct,
        confidence=confidence,
        violation=np.zeros(n, dtype=bool),
        model_cost=np.zeros(n),
    )
    policies = search_policies(
        batch,
        COST,
        target=0.95,
        alpha=0.05,
        human_accuracy=1.0,
        n_period=n,
        salt="test",
        category_sets=(("health",),),
        amount_cutoffs=(None,),
    )
    chosen, reason = select_policy(policies, 0.95)
    assert reason == "selected"
    assert chosen is not None
    assert chosen.family == "threshold"
    wrong_conf = confidence[~correct]
    assert chosen.params["tau"] == pytest.approx(float(wrong_conf.max()))
    route, counts = evaluate_policy(
        chosen, batch, COST, n_period=n, alpha=0.05, human_accuracy=1.0
    )
    assert counts["n_miss"] == chosen.dev_counts["n_miss"] == 0
    assert route.shape == (n,)
