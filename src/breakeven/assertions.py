"""State assertions: the same action can be legal in one workflow state and not in another.

Violations are computed on the agent's proposed action before oversight. The
state-check policy escalates any violating case. Residual risk is the violation
rate among cases the frozen policy still accepts, with a one-sided upper bound.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import yaml

from breakeven.dsl import compile_expr, evaluate
from breakeven.stats import clopper_pearson_upper


@dataclass(frozen=True)
class Assertion:
    id: str
    description: str
    when: str
    assert_: str

    def violated(self, context: dict) -> bool:
        if not evaluate(compile_expr(self.when), context):
            return False
        return not evaluate(compile_expr(self.assert_), context)


def load_assertions(path: str) -> list[Assertion]:
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    items = raw.get("assertions", raw if isinstance(raw, list) else [])
    out = []
    for item in items:
        out.append(
            Assertion(
                id=str(item["id"]),
                description=str(item.get("description", "")),
                when=str(item["when"]),
                assert_=str(item["assert"]),
            )
        )
    return out


def violation_mask(records: list[dict], assertions: list[Assertion]) -> np.ndarray:
    """True when any assertion fails. ``records`` are field dicts the DSL can read."""
    mask = np.zeros(len(records), dtype=bool)
    compiled = [(compile_expr(a.when), compile_expr(a.assert_), a.id) for a in assertions]
    for i, record in enumerate(records):
        for when, check, _aid in compiled:
            if evaluate(when, record) and not evaluate(check, record):
                mask[i] = True
                break
    return mask


def violation_summary(violations: np.ndarray, accepted: np.ndarray, alpha: float) -> dict:
    """Agent-level rate, and the residual rate on cases the policy accepts."""
    n = int(violations.size)
    k = int(violations.sum())
    acc = np.asarray(accepted, dtype=bool)
    n_acc = int(acc.sum())
    k_acc = int(violations[acc].sum()) if n_acc else 0
    return {
        "agent_n": n,
        "agent_violations": k,
        "agent_rate": (k / n) if n else float("nan"),
        "agent_upper": clopper_pearson_upper(k, n, alpha) if n else float("nan"),
        "residual_n": n_acc,
        "residual_violations": k_acc,
        "residual_rate": (k_acc / n_acc) if n_acc else 0.0,
        "residual_upper": clopper_pearson_upper(k_acc, n_acc, alpha) if n_acc else 0.0,
    }
