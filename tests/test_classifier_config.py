from __future__ import annotations

from copy import deepcopy
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment import validate_config as validator


def frozen_config():
    return json.loads(validator.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def test_frozen_config_is_complete_and_matches_shared_implementation():
    checks = validator.validate_config(frozen_config())
    assert checks["run_identity_count"] == 12
    assert checks["run_identities_unique"] is True
    assert checks["matched_dataset_settings_identical"] is True
    assert checks["base_precedes_matched_instruct"] is True


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda config: config["training"].update(learning_rate=3e-4), "learning_rate"),
        (lambda config: config["runs"].pop(), "exactly 12"),
        (lambda config: config["datasets"]["stanfordnlp/imdb"].update(excluded_splits=[]), "unlabeled"),
        (lambda config: config["checkpoints"]["base"]["model"].update(revision="main"), "immutable"),
        (
            lambda config: config["datasets"]["fancyzhx/ag_news"][
                "immutable_file_fingerprints"
            ]["train"].update(sha256="0" * 64),
            "fingerprint changed",
        ),
        (
            lambda config: config["runtime_protocol"]["gpu_admission"].update(
                minimum_free_memory_mib=10000
            ),
            "GPU admission",
        ),
    ],
)
def test_validator_rejects_protocol_drift(mutation, message):
    config = deepcopy(frozen_config())
    mutation(config)
    with pytest.raises(validator.ConfigValidationError, match=message):
        validator.validate_config(config)


def test_cli_writes_machine_readable_pass_evidence(tmp_path):
    status = validator.main(
        [str(validator.DEFAULT_CONFIG_PATH), "--artifacts-dir", str(tmp_path)]
    )
    output = tmp_path / "preflight" / "config_validation.json"
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert status == 0
    assert evidence["status"] == "PASS"
    assert evidence["checks"]["complete_matrix"]["cells"] == 12
    assert len(evidence["run_identities"]) == len(set(evidence["run_identities"])) == 12
