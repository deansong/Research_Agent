from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from classifier_experiment import validate_runtime


RUNTIME_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "classifier" / "runtime.json"


def load_config():
    return json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))


def test_frozen_runtime_protocol_resolves_both_effective_batch_combinations():
    resolved = validate_runtime.validate_protocol(load_config())

    assert resolved == {
        "256": {
            "per_device_train_microbatch_size": 4,
            "gradient_accumulation_steps": 8,
            "accelerators_per_run": 1,
            "effective_batch_size": 32,
        },
        "512": {
            "per_device_train_microbatch_size": 2,
            "gradient_accumulation_steps": 16,
            "accelerators_per_run": 1,
            "effective_batch_size": 32,
        },
    }


def test_effective_batch_mismatch_fails_closed():
    config = copy.deepcopy(load_config())
    config["batching"]["by_max_length"]["512"]["gradient_accumulation_steps"] = 8

    with pytest.raises(validate_runtime.RuntimeValidationError, match="effective batch size 32"):
        validate_runtime.validate_protocol(config)


def test_threshold_cannot_drift_from_frozen_admission_rule():
    config = copy.deepcopy(load_config())
    config["gpu_admission"]["minimum_free_memory_mib"] = 19_999

    with pytest.raises(validate_runtime.RuntimeValidationError, match="20000 MiB"):
        validate_runtime.validate_protocol(config)


def test_precision_fallback_is_only_predeclared_fp32():
    config = copy.deepcopy(load_config())
    config["precision"]["safe_fallback"] = "fp16"

    with pytest.raises(validate_runtime.RuntimeValidationError, match="FP32"):
        validate_runtime.validate_protocol(config)


def test_result_informed_compatibility_change_is_forbidden():
    config = copy.deepcopy(load_config())
    config["compatibility_change_control"]["may_be_based_on_model_or_data_results"] = True

    with pytest.raises(validate_runtime.RuntimeValidationError, match="result-informed"):
        validate_runtime.validate_protocol(config)


def test_hardware_evidence_uses_unique_identity(tmp_path):
    payload = {"schema_version": 1}
    first = validate_runtime._write_unique_hardware_evidence(tmp_path, payload)
    second = validate_runtime._write_unique_hardware_evidence(tmp_path, payload)

    assert first != second
    assert json.loads(first.read_text(encoding="utf-8")) == payload
    assert json.loads(second.read_text(encoding="utf-8")) == payload


def test_cuda_visibility_supports_ordinals_and_uuid_prefixes():
    gpu = {"index": 3, "uuid": "GPU-abc123"}

    assert validate_runtime._token_matches_gpu("3", gpu)
    assert validate_runtime._token_matches_gpu("GPU-abc", gpu)
    assert not validate_runtime._token_matches_gpu("2", gpu)
    assert not validate_runtime._token_matches_gpu("GPU-other", gpu)
