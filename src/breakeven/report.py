"""Offline dossier: one HTML file, a Markdown memo, and a measured profile.

Charts in the HTML are drawn as SVG so the file opens with no network. A
Vega-Lite spec for the same breakeven curve is embedded and written beside
the file; altair builds that spec, and the browser does not fetch Vega.
"""

from __future__ import annotations

import json
import math
from importlib.resources import files
from pathlib import Path

import altair as alt
import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from breakeven.models import CostModel


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def sanitize(value):
    import numpy as np

    if isinstance(value, dict):
        return {str(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, np.ndarray):
        return sanitize(value.tolist())
    if isinstance(value, np.floating):
        return _finite(float(value))
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, float):
        return _finite(value)
    return value


def build_profile(study: dict, evaluation: dict | None, *, command: str) -> dict:
    agents = {}
    for agent in study["agents"]:
        selected = agent["selected"]
        hold = selected["holdout"]
        baseline = hold["baseline_hours"] or 1.0
        agents[agent["id"]] = {
            "name": agent["name"],
            "summary": agent["summary"],
            "accuracy": agent["accuracy"],
            "n_correct": agent["n_correct"],
            "n": agent["n"],
            "status": agent["status"],
            "policy_id": selected["id"],
            "policy": selected["label"],
            "family": selected["family"],
            "params": selected["params"],
            "calibration_lcb": selected["dev_lcb"],
            "holdout_n": hold["n"],
            "holdout_successes": hold["successes"],
            "reliability": hold["reliability"],
            "lcb": hold["lcb"],
            "wilson_lcb": hold["wilson_lcb"],
            "automation_rate": hold["automation_rate"],
            "human_touch_rate": hold["human_touch_rate"],
            "miss_rate": hold["miss_rate"],
            "hours": hold["hours"],
            "baseline_hours": hold["baseline_hours"],
            "hours_ratio": hold["hours"] / baseline,
            "dollars": hold["dollars"],
            "baseline_dollars": hold["baseline_dollars"],
            "savings_dollars": hold["savings_dollars"],
            "savings_hours": hold["savings_hours"],
            "breakeven_cases": hold["breakeven_cases"],
            "bootstrap_savings_dollars_p05": (hold.get("bootstrap") or {}).get("p05"),
            "bootstrap_savings_dollars_p95": (hold.get("bootstrap") or {}).get("p95"),
            "a_h_min": hold["a_h_min"],
            "lcb_by_human_accuracy": selected["lcb_by_human_accuracy"],
            "assertions": hold["assertions"],
            "calibration_categories": agent["calibration_categories"],
            "book_categories": agent.get("book_categories", []),
            "agent_assertion_rate": agent["agent_assertion_rate"],
        }
    health = next(agent for agent in study["agents"] if agent["id"] == "health_blind")
    naive = health["naive"]["holdout"]
    naive_ratio = naive["hours"] / (naive["baseline_hours"] or 1.0)
    careful = agents["careful"]
    scattered = agents["scattered"]
    profile = {
        "command": command,
        "seed": study["seed"],
        "n_cases": study["n_cases"],
        "n_calibration": study["n_calibration"],
        "n_holdout": study["n_holdout"],
        "split_fraction": study["split_fraction"],
        "stratified": study["stratified"],
        "target_reliability": study["target"]["reliability"],
        "confidence_level": study["target"]["confidence"],
        "human_accuracy": study["target"]["human_accuracy"],
        "cost": study["cost"],
        "agents": agents,
        "trap": {
            "agent": "health_blind",
            "policy": health["naive"]["label"],
            "policy_id": health["naive"]["id"],
            "automation_rate": naive["automation_rate"],
            "human_touch_rate": naive["human_touch_rate"],
            "hours": naive["hours"],
            "baseline_hours": naive["baseline_hours"],
            "hours_ratio": naive_ratio,
            "hours_increase": naive_ratio - 1.0,
            "reliability": naive["reliability"],
            "lcb": naive["lcb"],
            "miss_rate": naive["miss_rate"],
            "qualified": bool(naive["lcb"] >= study["target"]["reliability"] and naive["savings_dollars"] > 0),
            "recommended_policy": health["selected"]["label"],
            "recommended_status": health["status"],
            "recommended_hours": health["selected"]["holdout"]["hours"],
            "recommended_hours_ratio": health["selected"]["holdout"]["hours"]
            / (health["selected"]["holdout"]["baseline_hours"] or 1.0),
            "recommended_lcb": health["selected"]["holdout"]["lcb"],
            "recommended_human_touch_rate": health["selected"]["holdout"]["human_touch_rate"],
        },
        "decision_flip": {
            "careful_accuracy": careful["accuracy"],
            "scattered_accuracy": scattered["accuracy"],
            "same_accuracy": abs(careful["accuracy"] - scattered["accuracy"]) < 1e-12,
            "careful_status": careful["status"],
            "scattered_status": scattered["status"],
            "careful_hours_ratio": careful["hours_ratio"],
            "scattered_hours_ratio": scattered["hours_ratio"],
            "brash_accuracy": agents["brash"]["accuracy"],
            "brash_status": agents["brash"]["status"],
            "brash_hours_ratio": agents["brash"]["hours_ratio"],
        },
        "evaluation": evaluation,
        "llm_runs": None,
    }
    return sanitize(profile)


def vega_breakeven(profile: dict, agent_id: str = "careful") -> dict:
    agent = profile["agents"][agent_id]
    # The curve itself is not in the summary profile. Callers pass the study
    # curve via a side channel stored on the profile under "_curve" when present.
    curve = profile.get("_curves", {}).get(agent_id)
    if curve is None:
        return {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": "Breakeven curve was not attached.",
            "data": {"values": []},
            "mark": "line",
        }
    rows = []
    for volume, dollars in zip(curve["volume"], curve["agent"], strict=True):
        rows.append({"volume": volume, "dollars": dollars, "series": "Agent system"})
    for volume, dollars in zip(curve["volume"], curve["human"], strict=True):
        rows.append({"volume": volume, "series": "All-human baseline", "dollars": dollars})
    frame = pd.DataFrame(rows)
    chart = (
        alt.Chart(frame)
        .mark_line()
        .encode(
            x=alt.X("volume:Q", title="Cases in the period"),
            y=alt.Y("dollars:Q", title="Fully loaded dollars"),
            color=alt.Color("series:N", title=None),
        )
        .properties(
            title=f"{agent['name']}: fully loaded dollars vs volume",
            width=640,
            height=280,
        )
    )
    return json.loads(chart.to_json())


def render_html(study: dict, profile: dict, vega: dict) -> str:
    payload = sanitize(
        {
            "study": {
                "n_cases": study["n_cases"],
                "n_calibration": study["n_calibration"],
                "n_holdout": study["n_holdout"],
                "seed": study["seed"],
                "target": study["target"],
                "cost": study["cost"],
            },
            "agents": study["agents"],
            "profile_trap": profile["trap"],
            "evaluation": profile.get("evaluation"),
        }
    )
    template_dir = files("breakeven") / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(disabled_extensions=("j2",)),
    )
    template = env.get_template("report.html.j2")
    payload_json = json.dumps(payload, allow_nan=False).replace("<", "\\u003c")
    vega_json = json.dumps(vega, allow_nan=False).replace("<", "\\u003c")
    return template.render(payload_json=payload_json, vega_json=vega_json)


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{100 * value:.1f}%"


def _num(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.0f}"


def render_dossier(profile: dict) -> str:
    cost = CostModel.model_validate(profile["cost"])
    target = profile["target_reliability"]
    conf = profile["confidence_level"]
    lines = []
    a = lines.append
    a("# Go-live dossier / 上线资格备忘")
    a("")
    a("Synthetic Acme claims. Every figure below is from the command in the footer.")
    a("合成的 Acme 理赔。下面每个数字都来自文末那条命令。")
    a("")
    a("## English")
    a("")
    a(
        f"Target reliability **{_pct(target)}**, one-sided Clopper–Pearson "
        f"confidence **{_pct(conf)}**, assumed human-review accuracy "
        f"**{_pct(profile['human_accuracy'])}**. "
        f"Calibration {profile['n_calibration']} / holdout {profile['n_holdout']}, "
        f"stratified by category, seed {profile['seed']}."
    )
    a("")
    a("| Agent | Accuracy | Verdict | Frozen policy | Holdout lower bound | Human touch | Hours vs all-human | Breakeven volume |")
    a("|---|---:|---|---|---:|---:|---:|---:|")
    en_status = {
        "go": "Go",
        "reliability_only": "Reliability only",
        "cost_uncertain": "Cost uncertain",
        "holdout_miss": "Holdout miss",
        "no_eligible_policy": "No policy",
    }
    for key in ("careful", "health_blind", "brash", "scattered"):
        agent = profile["agents"][key]
        a(
            f"| {agent['name']} | {_pct(agent['accuracy'])} | {en_status.get(agent['status'], agent['status'])} | {agent['policy']} "
            f"| {_pct(agent['lcb'])} | {_pct(agent['human_touch_rate'])} "
            f"| {_pct(agent['hours_ratio'] - 1)} | {_num(agent['breakeven_cases'], 0)} |"
        )
    a("")
    trap = profile["trap"]
    a("### The 90% plan")
    a("")
    a(
        f"On Health-blind, the lowest-confidence plan automates **{_pct(trap['automation_rate'])}** "
        f"of holdout cases and projects **{_num(trap['hours'])}** human hours against an all-human "
        f"**{_num(trap['baseline_hours'])}** ({_pct(trap['hours_increase'])} ). "
        f"Holdout lower bound **{_pct(trap['lcb'])}**. "
        f"The policy actually frozen for Health-blind is **{trap['recommended_policy']}** "
        f"({trap['recommended_status']}), at **{_pct(trap['recommended_human_touch_rate'])}** human touch "
        f"and **{_num(trap['recommended_hours'])}** hours "
        f"(ratio {_num(trap['recommended_hours_ratio'], 3)})."
    )
    a("")
    flip = profile["decision_flip"]
    a("### Same accuracy, opposite recommendation")
    a("")
    a(
        f"Careful and Scattered are both at accuracy **{_pct(flip['careful_accuracy'])}** "
        f"(equal: {flip['same_accuracy']}). Careful is **{flip['careful_status']}** "
        f"(hours ratio {_num(flip['careful_hours_ratio'], 3)}). "
        f"Scattered is **{flip['scattered_status']}** "
        f"(hours ratio {_num(flip['scattered_hours_ratio'], 3)}). "
        f"Brash is more accurate (**{_pct(flip['brash_accuracy'])}**) and is **{flip['brash_status']}** "
        f"(hours ratio {_num(flip['brash_hours_ratio'], 3)})."
    )
    a("")
    a("### Assumptions that the verdict depends on")
    a("")
    a(
        f"- All-human time {cost.hours_per_case_human:.2f} h/case, review {cost.hours_per_review:.2f} h, "
        f"exception {cost.hours_per_exception:.2f} h, dual review {cost.hours_per_dual_review:.2f} h, "
        f"audit {cost.hours_per_audit:.2f} h, correction of a shipped error {cost.hours_per_correction:.2f} h, "
        f"maintenance {cost.maintenance_hours:.1f} h per period, loaded rate {cost.currency} {cost.hourly_rate:.0f}/h, "
        f"model cost {cost.model_cost_per_case:.2f} per case."
    )
    a("- Reviewers are assumed perfect unless the human-accuracy sensitivity says otherwise. `a_h min` on each agent is the weakest constant review accuracy that still clears the target under the conditional bound.")
    a("- High-risk categories in the two-key menu (health, liability, and the amount cutoffs) are customer inputs. They were not chosen by looking at the holdout set.")
    a("- Cases are treated as iid. A book clustered by claimant or clinic needs a cluster-level bound this build does not compute.")
    a("- This is not an industry-average ROI. Change the cost sheet and the verdict can move. No live language model was called.")
    a("")
    a("## 中文")
    a("")
    a(
        f"可靠性目标 **{_pct(target)}**，单侧 Clopper–Pearson 置信水平 **{_pct(conf)}**，"
        f"人工复核准确率假定 **{_pct(profile['human_accuracy'])}**。"
        f"调参集 {profile['n_calibration']} 条，留出集 {profile['n_holdout']} 条，按类别分层，种子 {profile['seed']}。"
    )
    a("")
    a("| 代理 | 准确率 | 结论 | 冻结的策略 | 留出集下界 | 人工介入 | 相对全人工工时 | 盈亏平衡件数 |")
    a("|---|---:|---|---|---:|---:|---:|---:|")
    zh_name = {
        "careful": "稳妥",
        "health_blind": "健康险盲区",
        "brash": "冒进",
        "scattered": "散点",
    }
    zh_status = {
        "go": "可上线",
        "reliability_only": "只够可靠",
        "cost_uncertain": "成本不稳",
        "holdout_miss": "留出集未过",
        "no_eligible_policy": "没有策略",
    }
    for key in ("careful", "health_blind", "brash", "scattered"):
        agent = profile["agents"][key]
        a(
            f"| {zh_name[key]} | {_pct(agent['accuracy'])} | {zh_status.get(agent['status'], agent['status'])} | {agent['policy']} "
            f"| {_pct(agent['lcb'])} | {_pct(agent['human_touch_rate'])} "
            f"| {_pct(agent['hours_ratio'] - 1)} | {_num(agent['breakeven_cases'], 0)} |"
        )
    a("")
    a("### 自动化约九成的方案")
    a("")
    a(
        f"健康险盲区这个代理，按置信度最低的一档转人工，留出集上的自动化率是 **{_pct(trap['automation_rate'])}**，"
        f"按全月件数投影的人工工时是 **{_num(trap['hours'])}** 小时，全人工基线是 **{_num(trap['baseline_hours'])}** 小时"
        f"（相差 {_pct(trap['hours_increase'])}）。留出集可靠性下界 **{_pct(trap['lcb'])}**。"
        f"真正被冻结的策略是 **{trap['recommended_policy']}**（{trap['recommended_status']}），"
        f"人工介入 **{_pct(trap['recommended_human_touch_rate'])}**，工时 **{_num(trap['recommended_hours'])}** 小时"
        f"（比值 {_num(trap['recommended_hours_ratio'], 3)}）。"
    )
    a("")
    a("### 准确率相同，结论相反")
    a("")
    a(
        f"稳妥和散点的准确率都是 **{_pct(flip['careful_accuracy'])}**（是否相同：{flip['same_accuracy']}）。"
        f"稳妥的结论是 **{flip['careful_status']}**（工时比值 {_num(flip['careful_hours_ratio'], 3)}）。"
        f"散点的结论是 **{flip['scattered_status']}**（工时比值 {_num(flip['scattered_hours_ratio'], 3)}）。"
        f"冒进的准确率更高（**{_pct(flip['brash_accuracy'])}**），结论是 **{flip['brash_status']}**"
        f"（工时比值 {_num(flip['brash_hours_ratio'], 3)}）。"
    )
    a("")
    a("### 结论依赖的假设")
    a("")
    a(
        f"- 全人工 {cost.hours_per_case_human:.2f} 小时/件，复核 {cost.hours_per_review:.2f}，"
        f"异常 {cost.hours_per_exception:.2f}，双人复核 {cost.hours_per_dual_review:.2f}，"
        f"抽检 {cost.hours_per_audit:.2f}，漏检纠错 {cost.hours_per_correction:.2f}，"
        f"每期维护 {cost.maintenance_hours:.1f} 小时，全成本时薪 {cost.hourly_rate:.0f} {cost.currency}，"
        f"模型 {cost.model_cost_per_case:.2f}/件。"
    )
    a("- 默认把复核人员看成不会出错。每位代理上的 a_h min 是条件界仍能达到目标时，复核准确率可以低到什么程度。")
    a("- 双人复核菜单里的高风险类别（健康险、责任险，以及金额阈值）由客户事先给定，不是在留出集上搜出来的。")
    a("- 案例按独立同分布处理。如果案件按被保险人或机构成簇，需要簇水平的区间，这一版没有做。")
    a("- 这不是行业平均 ROI。成本表一改，结论就可能改。没有调用在线大模型。")
    a("")
    a("---")
    a("")
    a(f"Reproduced with `{profile['command']}`.")
    a("")
    return "\n".join(lines) + "\n"


def write_report_bundle(
    output: Path,
    study: dict,
    evaluation: dict | None,
    *,
    command: str,
) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    curves = {
        agent["id"]: agent["selected"]["curve"]
        for agent in study["agents"]
        if agent.get("selected")
    }
    profile = build_profile(study, evaluation, command=command)
    profile_for_vega = dict(profile)
    profile_for_vega["_curves"] = curves
    focus = "careful" if "careful" in curves else next(iter(curves))
    vega = vega_breakeven(profile_for_vega, focus)
    html = render_html(study, profile, vega)
    (output / "profile.json").write_text(json.dumps(profile, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "report.html").write_text(html, encoding="utf-8")
    (output / "dossier.md").write_text(render_dossier(profile), encoding="utf-8")
    (output / "breakeven.vl.json").write_text(json.dumps(vega, indent=2) + "\n", encoding="utf-8")
    (output / "qualification.json").write_text(
        json.dumps(sanitize(study), indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if evaluation is not None:
        (output / "evaluation.json").write_text(
            json.dumps(sanitize(evaluation), indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return profile
