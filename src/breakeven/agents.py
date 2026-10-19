"""Shadow agents.

Mock agents are deterministic functions of the case id and a fixed rule.
They never read ``decision_truth`` or ``fraud``. The OpenAI-compatible adapter
is optional and is not used by ``breakeven demo``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from breakeven.models import Action, Case, Decision, Prediction

DECISIONS: tuple[Decision, ...] = ("approve", "deny", "request_docs")


def _flip(truth: Decision) -> Decision:
    return DECISIONS[(DECISIONS.index(truth) + 1) % len(DECISIONS)]


def _unit(*parts: str) -> float:
    digest = hashlib.sha256("::".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _default_action(decision: Decision, state: str) -> Action:
    if decision == "approve" and state == "approved":
        return "pay"
    if decision == "approve":
        return "approve"
    if decision == "deny":
        return "deny"
    return "request_docs"


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    version: str
    description: str
    intended_accuracy: float


class MockAgent:
    """Deterministic shadow agent. ``version`` is part of the cache key.

    Exact-count agents assign errors across the cohort, so the runner caches
    them as a cohort rather than as independent per-case calls.
    """

    spec: AgentSpec
    cohort_level = True

    def predict(self, case: Case) -> Prediction:
        raise NotImplementedError("cohort-level mock agents implement predict_many")

    def predict_many(self, cases: list[Case]) -> list[Prediction]:
        return [self.predict(case) for case in cases]


class _RankedAgent(MockAgent):
    """Shared helpers for agents whose errors are a fixed count, not a coin flip.

    Exact counts make the demo's "both are 82%" comparison a property of the
    file, not a sample mean that moves with the seed.
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def _scores(self, cases: list[Case]) -> np.ndarray:
        return np.array([_unit(self.spec.agent_id, str(self.seed), c.case_id, "score") for c in cases])

    def _prediction(
        self,
        case: Case,
        *,
        correct: bool,
        confidence: float,
        illegal_pay: bool,
    ) -> Prediction:
        decision: Decision = case.decision_truth if correct else _flip(case.decision_truth)
        action: Action = "pay" if illegal_pay else _default_action(decision, case.state)
        return Prediction(
            case_id=case.case_id,
            agent=self.spec.agent_id,
            decision=decision,
            confidence=float(np.clip(confidence, 0.0, 1.0)),
            action=action,
            latency_ms=0.0,
            model_cost=0.0,
        )


class BrashAgent(_RankedAgent):
    """High accuracy, confidence unrelated to correctness."""

    spec = AgentSpec(
        agent_id="brash",
        version="1",
        description="Higher accuracy, confidence uncorrelated with being right.",
        intended_accuracy=0.88,
    )

    def predict_many(self, cases: list[Case]) -> list[Prediction]:
        n = len(cases)
        n_wrong = int(round((1.0 - self.spec.intended_accuracy) * n))
        scores = self._scores(cases)
        wrong_at = set(np.argsort(scores)[:n_wrong].tolist())
        out = []
        for i, case in enumerate(cases):
            confidence = 0.80 + 0.19 * _unit(self.spec.agent_id, case.case_id, "conf")
            illegal = _unit(self.spec.agent_id, case.case_id, "illegal") < 0.03
            out.append(
                self._prediction(
                    case,
                    correct=i not in wrong_at,
                    confidence=confidence,
                    illegal_pay=illegal,
                )
            )
        return out


class CarefulAgent(_RankedAgent):
    """Slightly lower accuracy. Every error is among the lowest confidences."""

    spec = AgentSpec(
        agent_id="careful",
        version="1",
        description="Lower accuracy, confidence ranks correctness.",
        intended_accuracy=0.82,
    )

    def predict_many(self, cases: list[Case]) -> list[Prediction]:
        n = len(cases)
        n_wrong = int(round((1.0 - self.spec.intended_accuracy) * n))
        ambiguity = self._scores(cases)
        wrong_at = set(np.argsort(-ambiguity)[:n_wrong].tolist())
        out = []
        for i, case in enumerate(cases):
            # Invert the rank score so errors sit at the bottom of the confidence range,
            # and keep a little per-case jitter inside the band so taus are unique.
            confidence = (1.0 - float(ambiguity[i])) * 0.97 + 0.01
            illegal = _unit(self.spec.agent_id, case.case_id, "illegal") < 0.01
            out.append(
                self._prediction(
                    case,
                    correct=i not in wrong_at,
                    confidence=confidence,
                    illegal_pay=illegal,
                )
            )
        return out


class HealthBlindAgent(_RankedAgent):
    """Same headline accuracy as Careful. Errors concentrate in health, at high confidence."""

    spec = AgentSpec(
        agent_id="health_blind",
        version="1",
        description="Same headline accuracy, systematic errors on health claims.",
        intended_accuracy=0.82,
    )

    def predict_many(self, cases: list[Case]) -> list[Prediction]:
        n = len(cases)
        health = [i for i, case in enumerate(cases) if case.category == "health"]
        other = [i for i, case in enumerate(cases) if case.category != "health"]
        # 60% of the health book is wrong; the rest of the error budget lands elsewhere.
        # For the canonical 2,000-claim book that is 240 + 120 = 360, i.e. 82%.
        n_wrong = int(round((1.0 - self.spec.intended_accuracy) * n))
        n_health_wrong = min(len(health), int(round(0.60 * len(health))))
        n_other_wrong = min(len(other), max(0, n_wrong - n_health_wrong))
        health_scores = np.array(
            [_unit(self.spec.agent_id, cases[i].case_id, "h") for i in health]
        )
        other_scores = np.array(
            [_unit(self.spec.agent_id, cases[i].case_id, "o") for i in other]
        ) if other else np.array([])
        wrong_at = set(np.array(health)[np.argsort(health_scores)[:n_health_wrong]].tolist())
        if n_other_wrong:
            wrong_at.update(np.array(other)[np.argsort(other_scores)[:n_other_wrong]].tolist())
        out = []
        for i, case in enumerate(cases):
            if case.category == "health":
                confidence = 0.90 + 0.09 * _unit(self.spec.agent_id, case.case_id, "conf")
                illegal = _unit(self.spec.agent_id, case.case_id, "illegal") < 0.18
            elif i in wrong_at:
                confidence = 0.30 + 0.20 * _unit(self.spec.agent_id, case.case_id, "conf")
                illegal = _unit(self.spec.agent_id, case.case_id, "illegal") < 0.01
            else:
                confidence = 0.55 + 0.31 * _unit(self.spec.agent_id, case.case_id, "conf")
                illegal = _unit(self.spec.agent_id, case.case_id, "illegal") < 0.01
            out.append(
                self._prediction(
                    case,
                    correct=i not in wrong_at,
                    confidence=confidence,
                    illegal_pay=illegal,
                )
            )
        return out


class ScatteredAgent(_RankedAgent):
    """Matched-accuracy control: Careful's accuracy, Brash's confidence."""

    spec = AgentSpec(
        agent_id="scattered",
        version="1",
        description="Careful's accuracy with confidence uncorrelated with correctness.",
        intended_accuracy=0.82,
    )

    def predict_many(self, cases: list[Case]) -> list[Prediction]:
        n = len(cases)
        n_wrong = int(round((1.0 - self.spec.intended_accuracy) * n))
        scores = self._scores(cases)
        wrong_at = set(np.argsort(scores)[:n_wrong].tolist())
        out = []
        for i, case in enumerate(cases):
            confidence = 0.55 + 0.44 * _unit(self.spec.agent_id, case.case_id, "conf")
            illegal = _unit(self.spec.agent_id, case.case_id, "illegal") < 0.02
            out.append(
                self._prediction(
                    case,
                    correct=i not in wrong_at,
                    confidence=confidence,
                    illegal_pay=illegal,
                )
            )
        return out


class OpenAICompatibleAgent:
    """Optional adapter. ``breakeven demo`` does not construct this class.

    The endpoint is any service that speaks the OpenAI chat-completions schema.
    Responses are parsed as JSON. Transport errors propagate to the caller; the
    shadow runner caches successes so a resumed run does not repeat them.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        agent_id: str = "openai",
        timeout: float = 60.0,
        client=None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.spec = AgentSpec(
            agent_id=agent_id,
            version=f"openai:{model}",
            description=f"OpenAI-compatible model {model}",
            intended_accuracy=float("nan"),
        )
        self.timeout = timeout
        self._client = client

    def predict(self, case: Case) -> Prediction:
        import httpx

        view = case.view().model_dump(mode="json")
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You adjudicate one insurance claim. "
                        "Reply with a single JSON object and no other text, "
                        'with keys decision (approve|deny|request_docs), '
                        "confidence (a number from 0 to 1), and "
                        "action (approve|deny|request_docs|pay|close)."
                    ),
                },
                {"role": "user", "content": json.dumps(view, sort_keys=True)},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        client = self._client or httpx.Client(timeout=self.timeout)
        close = client is not self._client
        try:
            response = client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        finally:
            if close:
                client.close()
        content = body["choices"][0]["message"]["content"]
        parsed = _parse_json_object(content)
        usage = body.get("usage") or {}
        # Price is unknown unless the customer sets model_cost_per_case.
        return Prediction(
            case_id=case.case_id,
            agent=self.spec.agent_id,
            decision=parsed["decision"],
            confidence=float(parsed["confidence"]),
            action=parsed.get("action", parsed["decision"]),
            latency_ms=0.0,
            model_cost=0.0,
            cached=False,
        )


def _parse_json_object(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model response did not contain a JSON object")
    data = json.loads(text[start : end + 1])
    if data.get("decision") not in DECISIONS:
        raise ValueError(f"decision {data.get('decision')!r} is not a known label")
    if not 0.0 <= float(data["confidence"]) <= 1.0:
        raise ValueError("confidence outside [0, 1]")
    return data


AGENTS: dict[str, type[MockAgent]] = {
    "brash": BrashAgent,
    "careful": CarefulAgent,
    "health_blind": HealthBlindAgent,
    "scattered": ScatteredAgent,
}


def build_mock(name: str, seed: int) -> MockAgent:
    try:
        cls = AGENTS[name]
    except KeyError as exc:
        raise KeyError(f"unknown mock agent {name!r}; choose from {sorted(AGENTS)}") from exc
    return cls(seed)
