from __future__ import annotations

import csv
import json
import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment import aggregate


def matrix_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for dataset_index, dataset in enumerate(aggregate.DATASETS):
        for role_index, (role, checkpoint) in enumerate(aggregate.CHECKPOINTS.items()):
            for seed_index, seed in enumerate(aggregate.SEEDS):
                accuracy = 0.70 + 0.10 * dataset_index + 0.02 * role_index + 0.01 * seed_index
                records.append(
                    {
                        "run_id": f"d{dataset_index}-{role}-s{seed}",
                        "dataset": dataset,
                        "checkpoint_role": role,
                        "checkpoint": checkpoint,
                        "seed": seed,
                        "attempt": 1,
                        "status": "complete",
                        "accuracy": accuracy,
                        "macro_f1": accuracy - 0.01,
                        "num_examples": 100,
                    }
                )
    return records


def test_exact_matrix_statistics_and_matched_differences():
    result = aggregate.aggregate_records(matrix_records())

    assert len(result["per_seed"]) == 12
    assert result["matrix"]["all_cells_successful"] is True
    ag_base = next(
        row for row in result["combination_statistics"]
        if row["dataset"] == "fancyzhx/ag_news" and row["checkpoint_role"] == "base"
    )
    assert ag_base["complete"] is True
    assert ag_base["accuracy_mean"] == pytest.approx(0.71)
    assert ag_base["accuracy_sample_std"] == pytest.approx(0.01)

    ag_pairs = [
        row for row in result["paired_differences"]
        if row["dataset"] == "fancyzhx/ag_news"
    ]
    assert [row["accuracy_difference"] for row in ag_pairs] == pytest.approx([0.02] * 3)
    ag_paired_stats = next(
        row for row in result["paired_statistics"]
        if row["dataset"] == "fancyzhx/ag_news"
    )
    assert ag_paired_stats["accuracy_difference_mean"] == pytest.approx(0.02)
    assert ag_paired_stats["accuracy_difference_sample_std"] == pytest.approx(0.0)


def test_missing_cell_is_rejected_instead_of_silently_averaged():
    records = matrix_records()
    records.pop()

    with pytest.raises(aggregate.AggregationError, match="exactly 12.*missing"):
        aggregate.aggregate_records(records)


def test_failed_cell_remains_explicit_and_invalidates_group_statistics():
    records = matrix_records()
    failed = records[0]
    failed.update(status="failed", reason="CUDA failure")
    failed.pop("accuracy")
    failed.pop("macro_f1")
    failed.pop("num_examples")

    result = aggregate.aggregate_records(records)
    cell = result["per_seed"][0]
    assert cell["status"] == "failed"
    assert cell["reason"] == "CUDA failure"
    assert cell["accuracy"] is None
    group = result["combination_statistics"][0]
    assert group["complete"] is False
    assert group["successful_runs"] == 2
    assert group["accuracy_mean"] is None
    assert group["accuracy_sample_std"] is None
    pair = result["paired_differences"][0]
    assert pair["complete"] is False
    assert pair["accuracy_difference"] is None
    paired_group = result["paired_statistics"][0]
    assert paired_group["complete"] is False
    assert paired_group["accuracy_difference_mean"] is None


def test_failed_retry_is_preserved_when_later_attempt_succeeds():
    records = matrix_records()
    first = records[0]
    first.update(status="failed", reason="worker preempted")
    first.pop("accuracy")
    first.pop("macro_f1")
    first.pop("num_examples")
    retry = {
        **next(record for record in matrix_records() if record["seed"] == 42),
        "run_id": "d0-base-s42-retry-2",
        "attempt": 2,
    }
    records.append(retry)

    result = aggregate.aggregate_records(records)
    assert result["matrix"]["all_cells_successful"] is True
    assert result["per_seed"][0]["attempt_count"] == 2
    attempts = [row for row in result["attempts"] if row["seed"] == 42 and row["dataset"] == aggregate.DATASETS[0] and row["checkpoint_role"] == "base"]
    assert [row["status"] for row in attempts] == ["failed", "complete"]


def test_export_writes_json_and_csv_tables(tmp_path):
    result = aggregate.aggregate_records(matrix_records())
    written = aggregate.export_tables(result, tmp_path)

    assert len(written) == 11
    assert json.loads((tmp_path / "aggregate.json").read_text())["matrix"]["expected_cells"] == 12
    with (tmp_path / "per_seed.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 12
    assert rows[0]["checkpoint_role"] == "base"


def test_duplicate_or_noncontiguous_attempts_are_rejected():
    records = matrix_records()
    duplicate = {**records[0], "run_id": "other-id"}
    with pytest.raises(aggregate.AggregationError, match="duplicate attempt"):
        aggregate.aggregate_records([*records, duplicate])

    skipped = {**records[0], "run_id": "attempt-three", "attempt": 3}
    with pytest.raises(aggregate.AggregationError, match="non-contiguous"):
        aggregate.aggregate_records([*records, skipped])
