from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import subprocess

import pytest

from scripts import run_classifier_matrix as launcher


GPU_HEADER = "0, GPU-a, A100, 40960, 35000, 0\n1, GPU-b, A100, 40960, 19000, 0\n"


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def inventory_runner(gpus: str, processes: str = ""):
    def run(command, **kwargs):
        if "--query-gpu=" in command[1]:
            return completed(gpus)
        if "--query-compute-apps=" in command[1]:
            return completed(processes)
        raise AssertionError(command)

    return run


def test_busy_and_low_memory_gpus_are_never_selected():
    records = launcher.query_gpu_inventory(
        runner=inventory_runner(
            GPU_HEADER,
            "GPU-a, 1234, trainer.py, 12000\n",
        ),
        executable="nvidia-smi",
    )
    classified = launcher.classify_inventory(records)

    assert launcher.select_idle_gpu(classified) is None
    assert classified[0]["rejection_reasons"] == ["active_compute_process"]
    assert classified[1]["rejection_reasons"] == ["below_free_memory_threshold"]


def test_nonzero_utilization_is_busy_even_without_reported_process():
    records = launcher.query_gpu_inventory(
        runner=inventory_runner("0, GPU-a, A100, 40960, 35000, 1\n"),
        executable="nvidia-smi",
    )
    classified = launcher.classify_inventory(records)
    assert classified[0]["eligible"] is False
    assert "nonzero_gpu_utilization" in classified[0]["rejection_reasons"]
    assert launcher.select_idle_gpu(classified) is None


def test_selection_uses_only_eligible_gpu_and_frozen_threshold_cannot_drop():
    records = launcher.query_gpu_inventory(
        runner=inventory_runner(GPU_HEADER), executable="nvidia-smi"
    )
    selected = launcher.select_idle_gpu(launcher.classify_inventory(records))
    assert selected is not None
    assert selected["uuid"] == "GPU-a"
    assert selected["eligible"] is True
    with pytest.raises(ValueError, match="cannot be below frozen"):
        launcher.classify_inventory(records, min_free_memory_mib=19_999)


def test_process_query_failure_is_not_treated_as_idle():
    def runner(command, **kwargs):
        if "--query-gpu=" in command[1]:
            return completed("0, GPU-a, A100, 40960, 35000, 0\n")
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(launcher.InventoryError, match="query failed"):
        launcher.query_gpu_inventory(runner=runner, executable="nvidia-smi")


def test_discovery_writes_all_classifications_below_artifact_root(tmp_path):
    check, candidate = launcher.discover_once(
        artifacts_dir=tmp_path,
        runner=inventory_runner(GPU_HEADER, "GPU-a, 55, train.py, 8000\n"),
        executable="nvidia-smi",
    )
    output = tmp_path / "preflight" / "gpu_inventory.json"
    evidence = json.loads(output.read_text(encoding="utf-8"))

    assert candidate is None
    assert check["eligible_candidate"] is None
    assert evidence["admission_rule"]["minimum_free_memory_mib"] == 20_000
    assert [gpu["eligible"] for gpu in evidence["checks"][0]["gpus"]] == [False, False]


def test_wait_retries_then_revalidates_and_claims_idle_gpu(tmp_path):
    responses = deque(
        [
            # First observation: active, so no revalidation query.
            "0, GPU-a, A100, 40960, 35000, 4\n",
            "",
            # Second observation and post-lock revalidation are both idle.
            "0, GPU-a, A100, 40960, 35000, 0\n",
            "",
            "0, GPU-a, A100, 40960, 34990, 0\n",
            "",
        ]
    )

    def runner(command, **kwargs):
        return completed(responses.popleft())

    sleeps = []
    claim = launcher.wait_for_idle_gpu(
        artifacts_dir=tmp_path,
        poll_interval_seconds=0.25,
        max_checks=2,
        runner=runner,
        executable="nvidia-smi",
        sleeper=sleeps.append,
    )
    try:
        assert claim.gpu["uuid"] == "GPU-a"
        assert claim.gpu["eligible"] is True
        assert sleeps == [0.25]
        evidence = json.loads(
            (tmp_path / "preflight" / "gpu_inventory.json").read_text(encoding="utf-8")
        )
        assert len(evidence["checks"]) == 2
        assert evidence["checks"][0]["claim_acquired"] is False
        assert evidence["checks"][1]["claim_acquired"] is True
    finally:
        claim.release()


def test_failed_post_lock_inventory_releases_claim_and_fails_closed(tmp_path, monkeypatch):
    responses = deque(
        [
            "0, GPU-a, A100, 40960, 35000, 0\n",
            "",
            subprocess.CalledProcessError(1, ["nvidia-smi"]),
        ]
    )
    acquired = []
    original_try_claim = launcher._try_claim

    def runner(command, **kwargs):
        response = responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return completed(response)

    def capture_claim(candidate, *, artifacts_dir):
        claim = original_try_claim(candidate, artifacts_dir=artifacts_dir)
        acquired.append(claim)
        return claim

    monkeypatch.setattr(launcher, "_try_claim", capture_claim)
    with pytest.raises(launcher.NoEligibleGpuError):
        launcher.wait_for_idle_gpu(
            artifacts_dir=tmp_path,
            max_checks=1,
            runner=runner,
            executable="nvidia-smi",
        )

    assert acquired[0] is not None
    assert acquired[0].lock_stream.closed
    evidence = json.loads(
        (tmp_path / "preflight" / "gpu_inventory.json").read_text(encoding="utf-8")
    )
    assert evidence["checks"][0]["query_succeeded"] is False
    assert evidence["checks"][0]["claim_acquired"] is False


def test_launch_sets_claimed_uuid_and_records_unique_allocation(tmp_path):
    gpu = {
        "index": 2,
        "uuid": "GPU-idle",
        "name": "A100",
        "memory_total_mib": 40960,
        "memory_free_mib": 35000,
        "utilization_gpu_percent": 0,
        "active_compute_processes": [],
        "eligible": True,
        "rejection_reasons": [],
    }
    lock_path = tmp_path / "manual.lock"
    lock_stream = lock_path.open("a+", encoding="utf-8")
    claim = launcher.GpuClaim(gpu=gpu, lock_stream=lock_stream, acquired_at_utc="now")
    observed = {}

    def command_runner(command, **kwargs):
        observed.update(kwargs["env"])
        return completed(returncode=7)

    result = launcher.launch_on_claimed_gpu(
        ["python", "worker.py"],
        claim=claim,
        artifacts_dir=tmp_path,
        environ={"KEEP": "yes", "CUDA_VISIBLE_DEVICES": "old"},
        command_runner=command_runner,
    )

    assert result == 7
    assert observed["CUDA_VISIBLE_DEVICES"] == "GPU-idle"
    assert observed["NVIDIA_VISIBLE_DEVICES"] == "GPU-idle"
    assert observed["KEEP"] == "yes"
    files = list((tmp_path / "allocations").glob("*.json"))
    assert len(files) == 1
    evidence = json.loads(files[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["returncode"] == 7
    assert evidence["gpu"]["eligible"] is True
    assert lock_stream.closed


def test_allocation_identity_collision_does_not_overwrite(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "_allocation_stem", lambda uuid: "same-id")
    existing = tmp_path / "allocations" / "same-id.json"
    existing.parent.mkdir()
    existing.write_text('{"original": true}\n', encoding="utf-8")
    evidence = {"status": "running"}

    created = launcher._write_new_allocation(tmp_path, "GPU-a", evidence)

    assert json.loads(existing.read_text(encoding="utf-8")) == {"original": True}
    assert created.name == "same-id-1.json"
    assert json.loads(created.read_text(encoding="utf-8"))["allocation_id"] == "same-id-1"
