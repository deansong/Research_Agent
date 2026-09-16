from __future__ import annotations

import json
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]
PREREGISTRATION = REPO_ROOT / "docs" / "CLASSIFIER_PREREGISTRATION.md"


def preregistration_text() -> str:
    return PREREGISTRATION.read_text(encoding="utf-8")


def decision_contract() -> dict:
    text = preregistration_text()
    match = re.search(
        r"<!-- BEGIN DECISION CONTRACT -->\s*```json\s*(.*?)\s*```\s*"
        r"<!-- END DECISION CONTRACT -->",
        text,
        flags=re.DOTALL,
    )
    assert match, "preregistration must contain its normative JSON decision contract"
    return json.loads(match.group(1))


def test_preregistration_names_frozen_scope_metrics_and_seeds():
    text = preregistration_text()
    contract = decision_contract()

    assert "frozen before comparative results are observed" in text.lower()
    assert contract["datasets"] == ["fancyzhx/ag_news", "stanfordnlp/imdb"]
    assert contract["checkpoints"] == {
        "base": "Qwen/Qwen2.5-1.5B",
        "instruct": "Qwen/Qwen2.5-1.5B-Instruct",
    }
    assert contract["seeds"] == [42, 43, 44]
    assert contract["primary_metric"] == "official_test_accuracy"
    assert contract["secondary_metric"] == "official_test_macro_f1"


def test_preregistration_locks_pairing_direction_and_limited_uncertainty():
    text = preregistration_text().lower()
    contract = decision_contract()

    assert "instruction minus base" in text
    assert contract["paired_difference"] == "instruct_minus_base"
    assert contract["paired_summary"] == (
        "arithmetic_mean_of_three_seed_matched_differences"
    )
    assert "sample standard deviation" in text
    assert "limited inferential power" in text
    assert contract["uncertainty"] == (
        "sample_standard_deviation_descriptive_only_limited_three_seed_power"
    )


def test_preregistration_limits_official_test_access_and_handles_failures():
    text = preregistration_text().lower()
    contract = decision_contract()

    assert "evaluated exactly once for each successful selected run" in text
    assert contract["official_test_evaluations_per_successful_selected_run"] == 1
    assert contract["requires_all_12_selected_cells_successful"] is True
    assert contract["missing_or_failed_cells_may_be_averaged"] is False
    assert contract["incomplete_matrix_status"] == "incomplete_no_recommendation"
    for required_failure_term in ("failed", "retry evidence", "silently omitted"):
        assert required_failure_term in text


def test_preregistration_encodes_all_three_mechanical_conclusions():
    text = preregistration_text().lower()
    contract = decision_contract()

    assert contract["accuracy_threshold"] == 0.01
    assert contract["threshold_unit"] == "proportion_equal_to_one_percentage_point"
    assert contract["outcomes"] == {
        "recommend_instruct": (
            "both_dataset_mean_accuracy_differences_greater_than_or_equal_to_"
            "positive_threshold"
        ),
        "recommend_base": (
            "both_dataset_mean_accuracy_differences_less_than_or_equal_to_"
            "negative_threshold"
        ),
        "no_winner": "all_other_complete_matrix_results",
    }
    assert "recommend instruction-tuned" in text
    assert "recommend base" in text
    assert "no consistent practical winner" in text
    assert "\\ge 0.01" in preregistration_text()
    assert "\\le -0.01" in preregistration_text()
    assert contract["macro_f1_may_override"] is False
