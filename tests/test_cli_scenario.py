"""The Acme demo is the regression for the product story."""

import json
from pathlib import Path

from breakeven.cli import main


def test_demo_decision_flip_and_trap(tmp_path: Path):
    output = tmp_path / "out"
    code = main(
        [
            "demo",
            "--output",
            str(output),
            "--cases",
            "2000",
            "--seed",
            "7",
            "--target",
            "0.95",
            "--boot",
            "80",
            "--skip-simulate",
        ]
    )
    assert code == 0
    profile = json.loads((output / "profile.json").read_text(encoding="utf-8"))
    agents = profile["agents"]
    assert agents["careful"]["accuracy"] == agents["scattered"]["accuracy"] == 0.82
    assert agents["health_blind"]["accuracy"] == 0.82
    assert agents["brash"]["accuracy"] == 0.88
    assert agents["careful"]["status"] == "go"
    assert agents["health_blind"]["status"] == "go"
    assert agents["scattered"]["status"] != "go"
    assert agents["brash"]["status"] != "go"
    assert agents["careful"]["hours_ratio"] < 0.5
    assert agents["scattered"]["human_touch_rate"] == 1
    assert "two_key" in agents["health_blind"]["family"] or "Two-key" in agents["health_blind"]["policy"]
    trap = profile["trap"]
    assert 0.85 <= trap["automation_rate"] <= 0.95
    assert trap["hours_increase"] > 0.10
    assert trap["lcb"] < 0.95
    assert trap["recommended_status"] == "go"
    html = (output / "report.html").read_text(encoding="utf-8")
    assert "src=\"http" not in html
    assert "href=\"http" not in html
    assert "application/vnd.vegalite+json" in html
    assert "Careful" in html and "Health-blind" in html
    dossier = (output / "dossier.md").read_text(encoding="utf-8")
    assert "上线资格备忘" in dossier
    assert "not an industry-average ROI" in dossier or "不是行业平均" in dossier
    assert (output / "predictions" / "careful.parquet").exists()
    assert (output / "breakeven.vl.json").exists()
