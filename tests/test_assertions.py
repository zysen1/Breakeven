from breakeven.assertions import Assertion, violation_mask
from breakeven.dsl import DSLError, eval_expr
import pytest


def test_expression_language():
    ctx = {"amount": 30000, "action": "pay", "state": "submitted", "documents_complete": False}
    assert eval_expr("amount > 25000 and action == 'pay'", ctx)
    assert eval_expr("state == 'approved' or state == 'payment_pending'", {"state": "approved", "amount": 1})
    assert not eval_expr("state == 'approved' or state == 'payment_pending'", ctx)
    assert eval_expr("action in ['pay', 'close']", ctx)
    with pytest.raises(DSLError):
        eval_expr("__import__('os')", ctx)


def test_state_assertion_fires_only_when_the_when_matches():
    assertion = Assertion(
        id="pay",
        description="",
        when="action == 'pay'",
        assert_="state == 'approved' or state == 'payment_pending'",
    )
    records = [
        {"action": "pay", "state": "submitted"},
        {"action": "pay", "state": "approved"},
        {"action": "deny", "state": "submitted"},
    ]
    mask = violation_mask(records, [assertion])
    assert mask.tolist() == [True, False, False]
