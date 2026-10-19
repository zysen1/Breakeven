"""Shadow runner: cache, resume, and a concurrent per-case path.

Mock agents that hit an exact accuracy assign errors across the whole cohort,
so they implement ``predict_many`` and are cached as a cohort. Per-case agents
(the OpenAI-compatible adapter, and any agent with ``predict``) run through a
thread pool. A second invocation with the same cache file does not call the
agent again. A partial cache resumes only the missing per-case rows.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from breakeven.models import Case, Prediction


def cache_key(agent_id: str, version: str, cohort: str, case_payload: dict) -> str:
    blob = json.dumps(
        {"agent": agent_id, "version": version, "cohort": cohort, "case": case_payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def cohort_id(cases: list[Case]) -> str:
    joined = "\n".join(sorted(case.case_id for case in cases))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _load_cache(path: Path) -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not path.exists():
        return found
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A crashed append can leave a partial final line. Skip it.
                continue
            found[row["key"]] = row
    return found


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def run_shadow(
    cases: list[Case],
    agent,
    cache_path: str | Path,
    *,
    workers: int = 4,
) -> list[Prediction]:
    path = Path(cache_path)
    cohort = cohort_id(cases)
    agent_id = agent.spec.agent_id
    version = agent.spec.version
    cached = _load_cache(path)
    keys = [
        cache_key(agent_id, version, cohort, case.view().cache_payload())
        for case in cases
    ]
    cohort_level = bool(getattr(agent, "cohort_level", False))

    if cohort_level:
        if keys and all(key in cached for key in keys):
            return [_from_row(cached[key], from_cache=True) for key in keys]
        predictions = agent.predict_many(cases)
        if len(predictions) != len(cases):
            raise RuntimeError("predict_many returned a different number of rows than cases")
        by_id = {prediction.case_id: prediction for prediction in predictions}
        ordered = []
        for case, key in zip(cases, keys, strict=True):
            prediction = by_id[case.case_id]
            if key not in cached:
                _append(path, {"key": key, "prediction": prediction.model_dump(mode="json")})
            ordered.append(prediction)
        return ordered

    ordered: list[Prediction | None] = [None] * len(cases)
    missing: list[int] = []
    for i, key in enumerate(keys):
        if key in cached:
            ordered[i] = _from_row(cached[key], from_cache=True)
        else:
            missing.append(i)
    if missing:
        workers = max(1, min(workers, len(missing)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(agent.predict, cases[i]): i for i in missing}
            for future in as_completed(futures):
                index = futures[future]
                prediction = future.result()
                prediction.cached = False
                _append(
                    path,
                    {
                        "key": keys[index],
                        "prediction": prediction.model_dump(mode="json"),
                    },
                )
                ordered[index] = prediction
    return [row for row in ordered if row is not None]


def _from_row(row: dict, *, from_cache: bool) -> Prediction:
    prediction = Prediction.model_validate(row["prediction"])
    prediction.cached = from_cache
    return prediction
