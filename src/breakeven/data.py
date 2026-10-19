"""Acme insurance-claims generator and case IO.

The book is synthetic. Labels are a deterministic function of a seed so a
shadow run can be replayed without a network connection. Category mix, fraud
base rates, and the adjudication rules are part of the fixture, not estimates
of any real insurer.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from breakeven.models import Case, Decision

CATEGORIES: list[tuple[str, int]] = [
    ("auto_collision", 700),
    ("property", 500),
    ("health", 400),
    ("liability", 250),
    ("travel", 150),
]
FRAUD_RATE = {
    "auto_collision": 0.08,
    "property": 0.06,
    "health": 0.10,
    "liability": 0.07,
    "travel": 0.12,
}
REGIONS = ("north", "south", "east", "west")
STATES = (
    "submitted",
    "investigating",
    "pending_docs",
    "approved",
    "denied",
    "paid",
)
STATE_P = np.array([0.40, 0.25, 0.10, 0.15, 0.07, 0.03])


def _categories(n: int) -> list[str]:
    """Scale the 2,000-claim mix to ``n`` without dropping a category when n >= 5."""
    base = sum(count for _, count in CATEGORIES)
    raw = [n * count / base for _, count in CATEGORIES]
    floors = [int(np.floor(x)) for x in raw]
    # Keep at least one row per category when there are enough rows.
    if n >= len(CATEGORIES):
        floors = [max(1, f) for f in floors]
    while sum(floors) > n:
        idx = int(np.argmax(floors))
        if floors[idx] > 1:
            floors[idx] -= 1
        else:
            break
    remainder = n - sum(floors)
    order = np.argsort([-(raw[i] - np.floor(raw[i])) for i in range(len(raw))])
    cursor = 0
    while remainder > 0:
        floors[int(order[cursor % len(floors)])] += 1
        remainder -= 1
        cursor += 1
    out: list[str] = []
    for (name, _), count in zip(CATEGORIES, floors, strict=True):
        out.extend([name] * count)
    return out


def generate_cases(n: int = 2000, seed: int = 7) -> list[Case]:
    if n < 1:
        raise ValueError("n must be positive")
    rng = np.random.default_rng(seed)
    categories = _categories(n)
    rng.shuffle(categories)
    cases: list[Case] = []
    for i, category in enumerate(categories):
        fraud = bool(rng.random() < FRAUD_RATE[category])
        documents_complete = bool(rng.random() < 0.90)
        # Lognormal amount in dollars, median near 1,800, with a long tail.
        amount = float(np.round(rng.lognormal(mean=7.5, sigma=0.85), 2))
        state = str(rng.choice(STATES, p=STATE_P))
        if not documents_complete and state == "paid":
            state = "pending_docs"
        region = str(rng.choice(REGIONS))
        prior = int(rng.integers(0, 6))
        tenure = float(np.round(rng.uniform(0.2, 28.0), 1))
        signal = float(rng.beta(7, 2) if fraud else rng.beta(1.6, 8))
        signal = float(np.clip(signal, 0.0, 1.0))
        exception = (not documents_complete) or amount >= 20000 or prior >= 4
        truth: Decision
        if fraud:
            truth = "deny"
        elif not documents_complete:
            truth = "request_docs"
        else:
            truth = "approve"
        cases.append(
            Case(
                case_id=f"ACME-{seed:04d}-{i:05d}",
                category=category,
                amount=amount,
                state=state,
                documents_complete=documents_complete,
                fraud_signal=round(signal, 4),
                exception_flag=exception,
                region=region,
                prior_claims=prior,
                claimant_tenure_years=tenure,
                decision_truth=truth,
                fraud=fraud,
            )
        )
    return cases


def cases_to_frame(cases: list[Case]) -> pd.DataFrame:
    return pd.DataFrame([case.model_dump(mode="json") for case in cases])


def write_cases(cases: list[Case], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = cases_to_frame(cases)
    if path.suffix == ".csv":
        frame.to_csv(path, index=False)
    elif path.suffix == ".parquet":
        frame.to_parquet(path, index=False)
    else:
        with path.open("w", encoding="utf-8") as handle:
            for row in frame.to_dict(orient="records"):
                handle.write(json.dumps(row) + "\n")


def read_cases(path: str | Path) -> list[Case]:
    path = Path(path)
    if path.suffix == ".csv":
        frame = pd.read_csv(path)
    elif path.suffix == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_json(path, lines=True)
    required = set(Case.model_fields)
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "case file is missing columns: " + ", ".join(sorted(missing))
        )
    cases = []
    for row in frame.to_dict(orient="records"):
        # pandas may yield numpy scalars; pydantic coerces them.
        cases.append(Case.model_validate(row))
    return cases


VIEW_FIELDS = (
    "case_id",
    "category",
    "amount",
    "state",
    "documents_complete",
    "fraud_signal",
    "exception_flag",
    "region",
    "prior_claims",
    "claimant_tenure_years",
)
TRUTH_FIELDS = ("decision_truth", "fraud")
