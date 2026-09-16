from __future__ import annotations

from hashlib import sha256
import json
import pathlib
import sys

import jsonschema
import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment import validate_artifacts


SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "configs"
    / "classifier"
    / "artifact_schema.json"
)
SHA256 = "0" * 64


def successful_manifest():
    return {
        "schema_version": 1,
        "identity": {
            "experiment_id": "qwen25_1p5b_lora_initialization_classifier_v1",
            "cell_id": "ag_news__seed-42__base",
            "run_id": "ag_news__seed-42__base__attempt-001",
            "attempt": 1,
            "pair_id": "ag_news__seed-42",
            "dataset": "fancyzhx/ag_news",
            "checkpoint_role": "base",
            "seed": 42,
            "run_directory": "runs/ag_news__seed-42__base__attempt-001",
        },
        "environment": {
            "evidence_file": "environment.json",
            "evidence_sha256": SHA256,
            "lock_file": "ml-requirements.lock",
            "lock_sha256": SHA256,
            "python_version": "3.11.0",
            "platform": "linux",
            "packages_match_lock": True,
        },
        "revisions": {
            "evidence_file": "revisions.json",
            "model_repo_id": "Qwen/Qwen2.5-1.5B",
            "model_revision": "8faed761d45a263340a0528343f099c05c9a4323",
            "tokenizer_repo_id": "Qwen/Qwen2.5-1.5B",
            "tokenizer_revision": "8faed761d45a263340a0528343f099c05c9a4323",
            "dataset_repo_id": "fancyzhx/ag_news",
            "dataset_revision": "eb185aade064a813bc0b7f42de02595523103ca4",
        },
        "data": {
            "evidence_file": "data.json",
            "dataset_fingerprint": "immutable-fingerprint",
            "source_file_fingerprints": {
                "train": {"path": "data/train.parquet", "sha256": SHA256, "bytes": 1},
                "test": {"path": "data/test.parquet", "sha256": SHA256, "bytes": 1},
            },
            "split_hashes": {"train": SHA256, "validation": SHA256, "official_test": SHA256},
            "split_sizes": {"train": 108000, "validation": 12000, "official_test": 7600},
            "split_method": "deterministic_stratified_90_10_from_official_train",
            "excluded_splits": [],
            "official_test_preserved": True,
        },
        "configuration": {
            "evidence_file": "config.json",
            "config_sha256": "fa47b44236cce63dd9e12abf9d15330fad9de9ad4ef584469979c0b982848908",
            "epochs": 3,
            "optimizer": "AdamW",
            "learning_rate": 0.0002,
            "weight_decay": 0.01,
            "scheduler": "linear",
            "warmup_ratio": 0.1,
            "effective_batch_size": 32,
            "gradient_clipping_max_norm": 1.0,
            "maximum_length": 256,
            "lora": {"rank": 16, "alpha": 32, "dropout": 0.05, "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"], "bias": "none"},
            "trainable_parameter_policy": "lora_adapters_and_sequence_classifier_head_only",
            "precision": "bf16",
            "tokenization": "raw_text_without_chat_template",
            "checkpoint_selection": "best_validation_accuracy_earliest_epoch_tie_break",
        },
        "gpu": {
            "evidence_file": "gpu.json", "physical_index": 0, "visible_index": 0,
            "uuid": "GPU-test", "name": "test", "driver_version": "1", "compute_capability": "8.0",
            "admission_checked_at_utc": "2026-09-16T00:00:00Z", "free_memory_at_admission_mib": 20000,
            "utilization_at_admission_percent": 0, "active_compute_processes": [], "met_frozen_admission_rule": True,
        },
        "memory": {"evidence_file": "memory.json", "peak_allocated_bytes": 1, "peak_reserved_bytes": 1, "peak_used_memory_mib": 1, "measurement_method": "torch"},
        "timing": {"evidence_file": "timing.json", "started_at_utc": "2026-09-16T00:00:00Z", "ended_at_utc": "2026-09-16T01:00:00Z", "wall_seconds": 3600, "training_seconds": 3500, "evaluation_seconds": 100},
        "history": {"evidence_file": "history.json", "epochs_completed": 3, "optimizer_steps": 10, "records_sha256": SHA256},
        "trainable_parameters": {
            "evidence_file": "trainable_parameters.json",
            "evidence_sha256": SHA256,
            "policy": "lora_adapters_and_sequence_classifier_head_only",
            "trainable_names": ["base.q_proj.lora_A.default.weight", "base.score.modules_to_save.default.weight"],
            "frozen_names": ["base.embed.weight"],
            "trainable_parameters": 20,
            "total_parameters": 100,
        },
        "checkpoint": {"evidence_file": "selected_checkpoint.json", "selected": True, "path": "checkpoints/epoch-002", "epoch": 2, "validation_accuracy": 0.75, "selection_metric": "validation_accuracy", "tie_break": "earliest_epoch", "content_sha256": SHA256},
        "metrics": {"evidence_file": "metrics.json", "official_test_evaluation_count": 1, "accuracy": 0.75, "macro_f1": 0.74, "num_examples": 7600},
        "outcome": {"evidence_file": "failure.json", "status": "successful", "failure_stage": None, "failure_type": None, "failure_message": None, "traceback_file": None, "retryable": False},
        "retry": {"evidence_file": "retry.json", "attempt": 1, "previous_attempt_run_id": None, "previous_attempt_directory": None, "retry_reason": None, "supersedes_previous_attempt": False, "next_attempt_run_id": None},
    }


def test_expected_matrix_is_complete_and_collision_free():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = validate_artifacts.build_expected_matrix(
        schema, schema_path="configs/classifier/artifact_schema.json"
    )

    assert manifest["mode"] == "expected_only"
    assert manifest["observed_run_artifacts_inspected"] is False
    assert manifest["observed_results_included"] is False
    assert manifest["matrix"]["expected_cells"] == 12
    assert manifest["matrix"]["distinct_run_directories"] == 12
    assert len(manifest["cells"]) == 12
    assert len({cell["run_id"] for cell in manifest["cells"]}) == 12
    paths = [path for cell in manifest["cells"] for path in cell["required_files"]]
    assert len(paths) == len(set(paths))
    assert all(cell["status"] == "expected_not_observed" for cell in manifest["cells"])


def test_schema_requires_every_requested_evidence_category():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    required = set(schema["required"])
    assert {
        "environment",
        "revisions",
        "data",
        "configuration",
        "gpu",
        "memory",
        "timing",
        "history",
        "trainable_parameters",
        "checkpoint",
        "metrics",
        "outcome",
        "retry",
    }.issubset(required)
    validate_artifacts.validate_schema(schema)
    assert schema["x-artifact-file-schemas"]["trainable_parameters.json"] == {
        "$ref": "#/$defs/trainable_parameters_evidence_file"
    }


def test_successful_manifest_satisfies_schema_and_cross_field_validator():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = successful_manifest()
    jsonschema.Draft202012Validator(schema).validate(manifest)
    validate_artifacts.validate_run_manifest(schema, manifest)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.pop("trainable_parameters"), "missing required categories"),
        (
            lambda value: value["trainable_parameters"].update(
                trainable_names=["base.q_proj.weight", "base.score.weight"]
            ),
            "trainable boundary",
        ),
        (
            lambda value: value["trainable_parameters"].pop("policy"),
            "policy",
        ),
        (
            lambda value: value["checkpoint"].update(
                selected=False, path=None, epoch=None, validation_accuracy=None, content_sha256=None
            ),
            "selected checkpoint",
        ),
        (
            lambda value: value["metrics"].update(
                official_test_evaluation_count=0, accuracy=None, macro_f1=None, num_examples=None
            ),
            "official-test evaluation",
        ),
        (
            lambda value: value["revisions"].update(
                model_repo_id="Qwen/Qwen2.5-1.5B-Instruct",
                model_revision="989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
            ),
            "frozen base revision",
        ),
        (
            lambda value: value["revisions"].update(
                dataset_revision="e6281661ce1c48d982bc483cf8a173c1bbeb5d31"
            ),
            "frozen dataset",
        ),
        (
            lambda value: value["configuration"].update(maximum_length=512),
            "maximum_length",
        ),
        (
            lambda value: value["identity"].update(pair_id="imdb__seed-42"),
            "pair_id",
        ),
        (
            lambda value: value["identity"].update(cell_id="invented-cell"),
            "not preregistered",
        ),
        (
            lambda value: value["retry"].update(attempt=2),
            "retry attempt",
        ),
        (
            lambda value: value["retry"].update(
                previous_attempt_run_id="invented", supersedes_previous_attempt=True
            ),
            "attempt 1",
        ),
    ],
)
def test_cross_field_validator_rejects_inconsistent_attempts(mutate, message):
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = successful_manifest()
    mutate(manifest)
    with pytest.raises(validate_artifacts.ArtifactSchemaError, match=message):
        validate_artifacts.validate_run_manifest(schema, manifest)


def test_schema_itself_rejects_success_without_selected_checkpoint_or_metrics():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = successful_manifest()
    manifest["checkpoint"].update(
        selected=False, path=None, epoch=None, validation_accuracy=None, content_sha256=None
    )
    manifest["metrics"].update(
        official_test_evaluation_count=0, accuracy=None, macro_f1=None, num_examples=None
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(manifest)


def test_schema_itself_rejects_unapproved_trainable_backbone_parameter():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = successful_manifest()
    manifest["trainable_parameters"]["trainable_names"].append(
        "base.embed.weight"
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(manifest)


def test_schema_itself_rejects_attempt_one_with_previous_retry_evidence():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = successful_manifest()
    manifest["retry"].update(
        previous_attempt_run_id="invented",
        previous_attempt_directory="runs/invented",
        retry_reason="invented",
        supersedes_previous_attempt=True,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["revisions"].update(
            model_repo_id="Qwen/Qwen2.5-1.5B-Instruct",
            model_revision="989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
        ),
        lambda value: value["revisions"].update(
            dataset_revision="e6281661ce1c48d982bc483cf8a173c1bbeb5d31"
        ),
        lambda value: value["configuration"].update(maximum_length=512),
        lambda value: value["identity"].update(pair_id="imdb__seed-42"),
        lambda value: value["identity"].update(seed=43),
        lambda value: value["identity"].update(cell_id="invented-cell"),
    ],
)
def test_schema_itself_rejects_frozen_identity_binding_drift(mutate):
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = successful_manifest()
    mutate(manifest)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(manifest)


def test_failed_attempt_cannot_contain_official_test_metrics():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    manifest = successful_manifest()
    manifest["outcome"].update(
        status="failed",
        failure_stage="training",
        failure_type="RuntimeError",
        failure_message="failure",
        retryable=True,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(manifest)
    with pytest.raises(validate_artifacts.ArtifactSchemaError, match="cannot carry"):
        validate_artifacts.validate_run_manifest(schema, manifest)


def trainable_evidence():
    return {
        "schema_version": 1,
        "status": "recorded",
        "policy": "lora_adapters_and_sequence_classifier_head_only",
        "trainable_names": [
            "base.q_proj.lora_A.default.weight",
            "base.score.modules_to_save.default.weight",
        ],
        "frozen_names": ["base.embed.weight"],
        "trainable_parameters": 20,
        "total_parameters": 100,
    }


def write_materialized_run(tmp_path, manifest, evidence):
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    run_dir = tmp_path / manifest["identity"]["run_id"]
    run_dir.mkdir()
    for filename in schema["x-artifact-layout"]["required_files"]:
        (run_dir / filename).write_text("{}\n", encoding="utf-8")
    evidence_bytes = (
        json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    (run_dir / "trainable_parameters.json").write_bytes(evidence_bytes)
    manifest["trainable_parameters"]["evidence_sha256"] = sha256(
        evidence_bytes
    ).hexdigest()
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return schema, run_dir


def test_materialized_trainable_parameters_file_is_schema_bound(tmp_path):
    schema, run_dir = write_materialized_run(
        tmp_path, successful_manifest(), trainable_evidence()
    )

    validate_artifacts.validate_run_directory(schema, run_dir)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.pop("policy"), "missing or unexpected"),
        (
            lambda value: value.update(
                trainable_names=["base.q_proj.weight", "base.score.weight"]
            ),
            "trainable boundary",
        ),
        (lambda value: value.update(status="not_recorded"), "recorded status"),
        (lambda value: value.update(unexpected=True), "missing or unexpected"),
    ],
)
def test_trainable_parameters_file_rejects_invalid_contents(mutate, message):
    evidence = trainable_evidence()
    mutate(evidence)
    with pytest.raises(validate_artifacts.ArtifactSchemaError, match=message):
        validate_artifacts.validate_trainable_parameters_evidence(evidence)


def test_materialized_trainable_parameters_hash_mismatch_is_rejected(tmp_path):
    manifest = successful_manifest()
    schema, run_dir = write_materialized_run(
        tmp_path, manifest, trainable_evidence()
    )
    manifest["trainable_parameters"]["evidence_sha256"] = "f" * 64
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )

    with pytest.raises(validate_artifacts.ArtifactSchemaError, match="hash"):
        validate_artifacts.validate_run_directory(schema, run_dir)


def test_materialized_trainable_parameter_summary_drift_is_rejected(tmp_path):
    manifest = successful_manifest()
    evidence = trainable_evidence()
    evidence["total_parameters"] = 101
    schema, run_dir = write_materialized_run(tmp_path, manifest, evidence)

    with pytest.raises(
        validate_artifacts.ArtifactSchemaError, match="total_parameters differs"
    ):
        validate_artifacts.validate_run_directory(schema, run_dir)


def test_collision_or_missing_cell_is_rejected():
    schema = validate_artifacts.load_schema(SCHEMA_PATH)
    schema["x-expected-matrix"]["cells"][1]["run_directory"] = schema[
        "x-expected-matrix"
    ]["cells"][0]["run_directory"]
    with pytest.raises(validate_artifacts.ArtifactSchemaError, match="collision"):
        validate_artifacts.validate_schema(schema)


def test_cli_writes_only_expected_evidence(tmp_path):
    output = tmp_path / "preflight" / "expected_matrix.json"
    assert (
        validate_artifacts.main(
            [
                "--schema",
                str(SCHEMA_PATH),
                "--expected-only",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["matrix"]["expected_cells"] == 12
    assert value["observed_run_artifacts_inspected"] is False
    assert not (tmp_path / "runs").exists()
