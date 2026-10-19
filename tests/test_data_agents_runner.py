"""Synthetic book, shadow cache, and the optional HTTP adapter."""

import json
from pathlib import Path

import httpx
import pytest

from breakeven.agents import (
    AgentSpec,
    BrashAgent,
    CarefulAgent,
    HealthBlindAgent,
    OpenAICompatibleAgent,
    ScatteredAgent,
)
from breakeven.data import generate_cases, read_cases, write_cases
from breakeven.models import Case, Prediction
from breakeven.runner import run_shadow
from breakeven.study import split_indices


def test_acme_book_and_three_behaviors_plus_matched_control():
    cases = generate_cases(2000, seed=7)
    by_id = {case.case_id: case for case in cases}

    def accuracy(agent):
        preds = agent.predict_many(cases)
        correct = [pred.decision == by_id[pred.case_id].decision_truth for pred in preds]
        return preds, sum(correct) / len(correct)

    careful_preds, careful_acc = accuracy(CarefulAgent(7))
    scattered_preds, scattered_acc = accuracy(ScatteredAgent(7))
    _brash_preds, brash_acc = accuracy(BrashAgent(7))
    health_preds, health_acc = accuracy(HealthBlindAgent(7))
    assert careful_acc == 0.82
    assert scattered_acc == 0.82
    assert health_acc == 0.82
    assert brash_acc == 0.88

    paired = list(zip(cases, careful_preds, strict=True))
    wrong = [pred.confidence for case, pred in paired if pred.decision != case.decision_truth]
    right = [pred.confidence for case, pred in paired if pred.decision == case.decision_truth]
    assert max(wrong) < min(right)

    health_cases = [(case, pred) for case, pred in zip(cases, health_preds, strict=True) if case.category == "health"]
    health_correct = sum(pred.decision == case.decision_truth for case, pred in health_cases)
    assert len(health_cases) == 400
    assert health_correct / len(health_cases) == 0.40

    # The case view handed to an agent has no label fields.
    view_fields = set(cases[0].view().model_dump())
    assert "decision_truth" not in view_fields
    assert "fraud" not in view_fields


def test_split_is_stratified_60_40_and_deterministic():
    cases = generate_cases(2000, seed=7)
    dev, hold = split_indices(cases, 0.60, seed=7)
    again, hold_again = split_indices(cases, 0.60, seed=7)
    assert dev == again and hold == hold_again
    assert set(dev).isdisjoint(hold)
    assert set(dev) | set(hold) == set(range(len(cases)))
    assert len(dev) / len(cases) == 0.60
    cats = {cases[i].category for i in dev}
    assert cats == {cases[i].category for i in hold} == {
        "auto_collision",
        "property",
        "health",
        "liability",
        "travel",
    }


def test_case_roundtrip_hides_nothing_the_file_needs(tmp_path: Path):
    cases = generate_cases(5, seed=1)
    path = tmp_path / "cases.jsonl"
    write_cases(cases, path)
    loaded = read_cases(path)
    assert [case.case_id for case in loaded] == [case.case_id for case in cases]
    for original, again in zip(cases, loaded, strict=True):
        assert again.decision_truth == original.decision_truth
        assert again.fraud == original.fraud
        assert again.fraud_signal == pytest.approx(original.fraud_signal)
        assert again.amount == pytest.approx(original.amount)


class _Counter:
    spec = AgentSpec(agent_id="counter", version="1", description="test", intended_accuracy=1)
    cohort_level = False

    def __init__(self):
        self.calls = 0

    def predict(self, case: Case) -> Prediction:
        self.calls += 1
        return Prediction(
            case_id=case.case_id,
            agent="counter",
            decision=case.decision_truth,
            confidence=0.5,
            action="approve",
        )


def test_cache_resume_skips_completed_cases(tmp_path: Path):
    cases = generate_cases(6, seed=2)
    cache = tmp_path / "cache.jsonl"
    agent = _Counter()
    first = run_shadow(cases, agent, cache, workers=4)
    assert agent.calls == 6
    assert len(first) == 6
    agent.calls = 0
    second = run_shadow(cases, agent, cache, workers=4)
    assert agent.calls == 0
    assert all(row.cached for row in second)

    lines = cache.read_text(encoding="utf-8").splitlines()
    cache.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
    agent.calls = 0
    third = run_shadow(cases, agent, cache, workers=2)
    assert agent.calls == 3
    assert [row.case_id for row in third] == [case.case_id for case in cases]


def test_openai_adapter_posts_the_view_and_parses_json():
    cases = generate_cases(1, seed=3)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"decision":"deny","confidence":0.42,"action":"deny"}\n```'
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    agent = OpenAICompatibleAgent(
        base_url="http://models.internal/v1",
        api_key="test-key",
        model="mock",
        client=client,
    )
    prediction = agent.predict(cases[0])
    assert prediction.decision == "deny"
    assert prediction.confidence == 0.42
    assert seen["url"].endswith("/chat/completions")
    user = json.loads(seen["body"]["messages"][1]["content"])
    assert "decision_truth" not in user
    assert "fraud" not in user
    assert user["case_id"] == cases[0].case_id
