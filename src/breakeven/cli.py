"""Command line for Breakeven.

``demo`` and ``simulate`` stay offline. ``run`` will call an OpenAI-compatible
endpoint only when the config names one and an API key is present; the Acme
demo never sets that.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import webbrowser
from importlib.resources import files
from pathlib import Path

import pandas as pd
import yaml

from breakeven import __version__
from breakeven.assertions import load_assertions
from breakeven.data import generate_cases, read_cases, write_cases
from breakeven.evaluate import run_evaluation
from breakeven.models import CostModel, Target
from breakeven.report import write_report_bundle
from breakeven.study import run_predictions, run_qualification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="breakeven", description="Qualify an agent deployment before it goes live.")
    parser.add_argument("--version", action="version", version=f"breakeven {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init_p = sub.add_parser("init", help="Write a config template and the Acme assertion file.")
    init_p.add_argument("--dir", default=".")

    run_p = sub.add_parser("run", help="Shadow-replay cases with the configured agents.")
    run_p.add_argument("--config", required=True)

    qual_p = sub.add_parser("qualify", help="Search policies on the calibration split and check the holdout.")
    qual_p.add_argument("--config", required=True)

    rep_p = sub.add_parser("report", help="Render the HTML dossier from a qualification file.")
    rep_p.add_argument("--config", required=True)
    rep_p.add_argument("--open", action="store_true")

    demo_p = sub.add_parser("demo", help="Run the offline Acme study end to end.")
    demo_p.add_argument("--output", default="breakeven-out")
    demo_p.add_argument("--cases", type=int, default=2000)
    demo_p.add_argument("--seed", type=int, default=7)
    demo_p.add_argument("--target", type=float, default=0.95)
    demo_p.add_argument("--reps", type=int, default=1000)
    demo_p.add_argument("--boot", type=int, default=400)
    demo_p.add_argument("--skip-simulate", action="store_true")

    sim_p = sub.add_parser("simulate", help="Coverage of the lower bound, and the gap to an oracle threshold.")
    sim_p.add_argument("--output", default="breakeven-out")
    sim_p.add_argument("--reps", type=int, default=1000)

    args = parser.parse_args(argv)
    if args.cmd == "init":
        return cmd_init(Path(args.dir))
    if args.cmd == "run":
        return cmd_run(Path(args.config))
    if args.cmd == "qualify":
        return cmd_qualify(Path(args.config))
    if args.cmd == "report":
        return cmd_report(Path(args.config), open_browser=args.open)
    if args.cmd == "demo":
        return cmd_demo(args)
    if args.cmd == "simulate":
        return cmd_simulate(Path(args.output), args.reps)
    return 2


def _read_config(path: Path) -> tuple[dict, Path]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw, path.parent.resolve()


def _cost_and_target(raw: dict) -> tuple[CostModel, Target]:
    return CostModel.model_validate(raw["cost"]), Target.model_validate(raw["target"])


def cmd_init(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    defaults = files("breakeven") / "defaults"
    for name in ("breakeven.yaml", "assertions.yaml"):
        destination = directory / name
        if destination.exists():
            print(f"keep {destination}")
            continue
        destination.write_text((defaults / name).read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {destination}")
    print("Next: put labeled cases at cases.jsonl (or run `breakeven demo`), then `breakeven run --config breakeven.yaml`.")
    return 0


def _ensure_cases(raw: dict, root: Path) -> list:
    dataset = raw["dataset"]
    path = root / dataset["path"]
    if path.exists():
        return read_cases(path)
    if dataset.get("generator") == "acme":
        cases = generate_cases(int(dataset.get("n_cases", 2000)), int(raw.get("seed", 7)))
        write_cases(cases, path)
        print(f"generated {len(cases)} Acme cases at {path}")
        return cases
    raise SystemExit(f"case file not found: {path}")


def cmd_run(config_path: Path) -> int:
    raw, root = _read_config(config_path)
    cases = _ensure_cases(raw, root)
    out = (root / raw.get("output_dir", ".")).resolve()
    cache = out / "cache"
    names = list(raw.get("agents") or ["careful"])
    predictions = run_predictions(
        cases,
        names,
        seed=int(raw.get("seed", 7)),
        cache_dir=cache,
        workers=int(raw.get("workers", 4)),
    )
    pred_dir = out / "predictions"
    pred_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in predictions.items():
        frame = pd.DataFrame([row.model_dump(mode="json") for row in rows])
        frame.to_parquet(pred_dir / f"{name}.parquet", index=False)
        print(f"{name}: {len(rows)} predictions, {int(frame['cached'].sum()) if 'cached' in frame else 0} from cache")
    return 0


def cmd_qualify(config_path: Path) -> int:
    raw, root = _read_config(config_path)
    cost, target = _cost_and_target(raw)
    cases = _ensure_cases(raw, root)
    out = (root / raw.get("output_dir", ".")).resolve()
    names = list(raw.get("agents") or ["careful"])
    predictions = run_predictions(
        cases,
        names,
        seed=int(raw.get("seed", 7)),
        cache_dir=out / "cache",
        workers=int(raw.get("workers", 4)),
    )
    assertions = load_assertions(root / raw.get("assertions", "assertions.yaml"))
    study = run_qualification(
        cases,
        predictions,
        cost=cost,
        target=target,
        assertions=assertions,
        seed=int(raw.get("seed", 7)),
        n_boot=int(raw.get("bootstrap", 400)),
    )
    command = f"breakeven qualify --config {config_path}"
    profile = write_report_bundle(out, study, None, command=command)
    _print_summary(profile)
    return 0


def cmd_report(config_path: Path, *, open_browser: bool) -> int:
    raw, root = _read_config(config_path)
    out = (root / raw.get("output_dir", ".")).resolve()
    html = out / "report.html"
    if not html.exists():
        cmd_qualify(config_path)
    if open_browser:
        webbrowser.open(html.resolve().as_uri())
    print(html)
    return 0


def cmd_simulate(output: Path, reps: int) -> int:
    output.mkdir(parents=True, exist_ok=True)
    evaluation = run_evaluation(reps=reps, e2e_reps=reps)
    path = output / "evaluation.json"
    import json

    path.write_text(json.dumps(evaluation, indent=2) + "\n", encoding="utf-8")
    e2e = evaluation["end_to_end"]
    print(
        f"binomial rows {len(evaluation['binomial'])}; "
        f"end-to-end coverage {e2e['coverage']:.3f} on {e2e['reported']} reported reps; "
        f"mean relative variable-cost gap {e2e['mean_relative_variable_cost_gap']:.3f}"
    )
    print(path)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    defaults = files("breakeven") / "defaults"
    assertions_text = (defaults / "assertions.yaml").read_text(encoding="utf-8")
    (output / "assertions.yaml").write_text(assertions_text, encoding="utf-8")
    config = {
        "seed": args.seed,
        "dataset": {"path": "cases.jsonl", "n_cases": args.cases, "generator": "acme"},
        "agents": ["brash", "careful", "health_blind", "scattered"],
        "target": {"reliability": args.target, "confidence": 0.95, "human_accuracy": 1.0},
        "cost": yaml.safe_load((defaults / "breakeven.yaml").read_text(encoding="utf-8"))["cost"],
        "assertions": "assertions.yaml",
        "output_dir": ".",
        "workers": 4,
        "bootstrap": args.boot,
    }
    config["cost"]["human_accuracy"] = 1.0
    (output / "breakeven.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    cases = generate_cases(args.cases, args.seed)
    write_cases(cases, output / "cases.jsonl")
    cost, target = _cost_and_target(config)
    predictions = run_predictions(
        cases,
        config["agents"],
        seed=args.seed,
        cache_dir=output / "cache",
        workers=4,
    )
    pred_dir = output / "predictions"
    pred_dir.mkdir(parents=True)
    for name, rows in predictions.items():
        pd.DataFrame([row.model_dump(mode="json") for row in rows]).to_parquet(
            pred_dir / f"{name}.parquet", index=False
        )
    assertions = load_assertions(output / "assertions.yaml")
    study = run_qualification(
        cases,
        predictions,
        cost=cost,
        target=target,
        assertions=assertions,
        seed=args.seed,
        n_boot=args.boot,
    )
    evaluation = None if args.skip_simulate else run_evaluation(reps=args.reps, e2e_reps=args.reps)
    command = (
        f"breakeven demo --output {args.output} --cases {args.cases} "
        f"--seed {args.seed} --target {args.target} --reps {args.reps} --boot {args.boot}"
    )
    profile = write_report_bundle(output, study, evaluation, command=command)
    _print_summary(profile)
    if evaluation is not None:
        e2e = evaluation["end_to_end"]
        print(
            f"coverage end-to-end {e2e['coverage']:.3f} "
            f"(reported {e2e['reported']}/{e2e['reps']}); "
            f"oracle tau {e2e['oracle_tau']:.3f}; "
            f"mean relative variable-cost gap {e2e['mean_relative_variable_cost_gap']:.3f}"
        )
    print(output / "report.html")
    return 0


def _print_summary(profile: dict) -> None:
    print(
        f"cases {profile['n_cases']}  calibration {profile['n_calibration']}  "
        f"holdout {profile['n_holdout']}  target {profile['target_reliability']:.0%}"
    )
    for key, agent in profile["agents"].items():
        print(
            f"  {key:14} acc {agent['accuracy']:.1%}  {agent['status']:20}  "
            f"LCB {agent['lcb']:.1%}  touch {agent['human_touch_rate']:.1%}  "
            f"hours {agent['hours']:.1f}/{agent['baseline_hours']:.1f}"
        )
    trap = profile["trap"]
    print(
        f"trap automation {trap['automation_rate']:.1%}  "
        f"hours {trap['hours']:.1f} vs {trap['baseline_hours']:.1f}  "
        f"({trap['hours_increase']:+.1%})"
    )


if __name__ == "__main__":
    sys.exit(main())
