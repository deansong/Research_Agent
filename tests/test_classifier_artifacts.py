from __future__ import annotations

import json
import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment import artifacts


def identity(run_id: str = "ag-news-base-seed-42-attempt-1") -> artifacts.RunIdentity:
    return artifacts.RunIdentity(
        run_id=run_id,
        dataset="fancyzhx/ag_news",
        checkpoint="Qwen/Qwen2.5-1.5B",
        seed=42,
        attempt=1,
    )


def complete_payloads(writer: artifacts.RunArtifacts) -> None:
    writer.write_history(
        [
            {"epoch": 1, "mean_train_loss": 0.8, "validation_accuracy": 0.70},
            {"epoch": 2, "mean_train_loss": 0.5, "validation_accuracy": 0.75},
        ]
    )
    writer.write_metrics(
        {"accuracy": 0.75, "macro_f1": 0.7333333333333334, "num_examples": 4}
    )
    writer.write_trainable_parameters(
        {
            "trainable_names": ["base.q_proj.lora_A", "score.weight"],
            "frozen_names": ["embed.weight"],
            "trainable_parameters": 20,
            "total_parameters": 100,
        }
    )
    writer.write_selected_checkpoint(
        {
            "path": "checkpoints/epoch-002",
            "epoch": 2,
            "validation_accuracy": 0.75,
            "metric": "validation_accuracy",
            "tie_break": "earliest_epoch",
        }
    )


def test_accuracy_and_macro_f1_fixtures():
    predictions = [0, 0, 1, 2, 2, 2]
    labels = [0, 1, 1, 2, 0, 2]

    assert artifacts.accuracy(predictions, labels) == pytest.approx(4 / 6)
    # Class F1 values are 1/2, 2/3, and 4/5.
    assert artifacts.macro_f1(predictions, labels, num_labels=3) == pytest.approx(
        (0.5 + 2 / 3 + 0.8) / 3
    )
    assert artifacts.classification_metrics(
        predictions, labels, num_labels=3
    ) == pytest.approx(
        {"accuracy": 4 / 6, "macro_f1": (0.5 + 2 / 3 + 0.8) / 3, "num_examples": 6}
    )


def test_metrics_reject_empty_mismatched_and_out_of_range_inputs():
    with pytest.raises(ValueError, match="at least one"):
        artifacts.classification_metrics([], [], num_labels=2)
    with pytest.raises(ValueError, match="same length"):
        artifacts.classification_metrics([0], [0, 1], num_labels=2)
    with pytest.raises(ValueError, match="outside"):
        artifacts.classification_metrics([2], [0], num_labels=2)


def test_new_run_has_required_schema_and_is_explicitly_incomplete(tmp_path):
    run_dir = tmp_path / "run"
    writer = artifacts.RunArtifacts.create(
        run_dir,
        identity=identity(),
        config={"epochs": 3, "model_revision": "immutable-model-commit"},
        metadata={"dataset_revision": "immutable-dataset-commit"},
    )

    assert {path.name for path in run_dir.iterdir()} == set(artifacts.REQUIRED_FILES)
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "incomplete"
    assert metadata["identity"] == identity().to_dict()
    with pytest.raises(artifacts.ArtifactValidationError, match="not complete"):
        artifacts.validate_run_artifacts(run_dir)

    writer.mark_incomplete("worker was preempted before epoch 1")
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "incomplete"
    assert metadata["reason"] == "worker was preempted before epoch 1"


def test_complete_run_schema_includes_checkpoint_selection(tmp_path):
    writer = artifacts.RunArtifacts.create(
        tmp_path / "run", identity=identity(), config={"epochs": 3}
    )
    complete_payloads(writer)
    writer.mark_complete()

    manifest = artifacts.validate_run_artifacts(
        tmp_path / "run", expected_identity=identity()
    )
    selected = json.loads(
        (tmp_path / "run" / "selected_checkpoint.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "complete"
    assert selected["epoch"] == 2
    assert selected["tie_break"] == "earliest_epoch"
    assert selected["validation_accuracy"] == 0.75


def test_schema_validation_fails_closed(tmp_path):
    writer = artifacts.RunArtifacts.create(
        tmp_path / "run", identity=identity(), config={"epochs": 3}
    )
    with pytest.raises(artifacts.ArtifactValidationError, match=r"\[0, 1\]"):
        writer.write_metrics(
            {"accuracy": 1.1, "macro_f1": 0.8, "num_examples": 10}
        )
    with pytest.raises(artifacts.ArtifactValidationError, match="strictly increasing"):
        writer.write_history(
            [
                {"epoch": 1, "mean_train_loss": 1.0, "validation_accuracy": 0.5},
                {"epoch": 1, "mean_train_loss": 0.9, "validation_accuracy": 0.6},
            ]
        )
    with pytest.raises(artifacts.ArtifactValidationError, match="cannot complete"):
        writer.mark_complete()


def test_atomic_write_keeps_previous_document_if_replace_fails(tmp_path, monkeypatch):
    destination = tmp_path / "metrics.json"
    artifacts.atomic_write_json(destination, {"generation": 1})

    def fail_replace(source, target):
        raise OSError("injected replacement failure")

    monkeypatch.setattr(artifacts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        artifacts.atomic_write_json(destination, {"generation": 2})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"generation": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_never_overwrites_existing_or_different_run_identity(tmp_path):
    run_dir = tmp_path / "run"
    first = identity()
    artifacts.RunArtifacts.create(run_dir, identity=first, config={"epochs": 3})

    with pytest.raises(FileExistsError, match="refusing to overwrite existing"):
        artifacts.RunArtifacts.create(run_dir, identity=first, config={"epochs": 3})

    second = identity("ag-news-base-seed-43-attempt-1")
    second = artifacts.RunIdentity(
        run_id=second.run_id,
        dataset=second.dataset,
        checkpoint=second.checkpoint,
        seed=43,
        attempt=1,
    )
    with pytest.raises(artifacts.RunIdentityError, match="different run identity"):
        artifacts.RunArtifacts.create(run_dir, identity=second, config={"epochs": 3})

    with pytest.raises(artifacts.RunIdentityError, match="different run identity"):
        artifacts.RunArtifacts.open(run_dir, identity=second)


def test_failed_attempt_is_retained_and_retry_needs_distinct_identity(tmp_path):
    root = tmp_path / "artifacts"
    first = artifacts.create_run_artifacts(root, identity=identity(), config={"epochs": 3})
    first.mark_failed("CUDA out of memory")
    retry_identity = artifacts.RunIdentity(
        run_id="ag-news-base-seed-42-attempt-2",
        dataset="fancyzhx/ag_news",
        checkpoint="Qwen/Qwen2.5-1.5B",
        seed=42,
        attempt=2,
    )
    retry = artifacts.create_run_artifacts(
        root, identity=retry_identity, config={"epochs": 3}
    )

    assert first.run_dir != retry.run_dir
    assert json.loads((first.run_dir / "metadata.json").read_text())["status"] == "failed"
    assert json.loads((retry.run_dir / "metadata.json").read_text())["status"] == "incomplete"
