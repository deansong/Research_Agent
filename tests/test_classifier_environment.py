from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment import environment


REQUIRED_PACKAGES = {
    "torch",
    "transformers",
    "datasets",
    "peft",
    "accelerate",
    "scikit-learn",
    "numpy",
    "pandas",
    "matplotlib",
    "seaborn",
}


def test_ml_lock_has_exact_required_pins():
    pins = environment.read_pins()
    assert set(pins) == REQUIRED_PACKAGES
    assert all(version and not version.startswith((">", "<", "~", "=")) for version in pins.values())


def test_environment_record_has_packages_and_cuda_visibility(monkeypatch):
    monkeypatch.setattr(environment, "_run_nvidia_smi", lambda: ({"query_succeeded": True}, []))
    monkeypatch.setattr(
        environment,
        "_torch_record",
        lambda: {"cuda_available": False, "visible_device_count": 0},
    )

    record = environment.build_environment_record(
        environ={"CUDA_VISIBLE_DEVICES": "2"}
    )

    assert set(record["lock_file"]["packages"]) == REQUIRED_PACKAGES
    assert record["python"]["version"]
    visibility = record["cuda"]["visibility_environment"]
    assert visibility["CUDA_VISIBLE_DEVICES"] == {"is_set": True, "value": "2"}
    assert visibility["NVIDIA_VISIBLE_DEVICES"] == {"is_set": False, "value": None}
    for package in record["lock_file"]["packages"].values():
        assert set(package) == {"pinned_version", "installed_version", "matches_pin"}


def test_environment_record_is_written_only_below_artifact_root(tmp_path):
    output = environment.write_environment_record(
        {"schema_version": 1}, artifacts_dir=tmp_path
    )
    assert output == tmp_path / "preflight" / "environment.json"
    assert json.loads(output.read_text(encoding="utf-8")) == {"schema_version": 1}


def test_lock_rejects_non_exact_requirements(tmp_path):
    lock = tmp_path / "bad.lock"
    lock.write_text("torch>=2.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exact pin"):
        environment.read_pins(lock)
