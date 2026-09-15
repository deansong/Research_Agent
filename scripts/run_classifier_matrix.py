#!/usr/bin/env python3
"""Fail-closed idle-GPU launcher for the classifier experiment matrix.

The frozen admission rule is at least 20,000 MiB free memory, zero reported
GPU utilization, and no process in NVIDIA's compute-process inventory.  A GPU
is ineligible when either nvidia-smi query fails or a field cannot be parsed.
The threshold may be raised for a reproduction, but it must not be lowered for
the comparative experiment merely because no GPU is currently available.

The launcher also takes an advisory per-GPU file lock and repeats the inventory
after obtaining it.  This prevents cooperating launcher instances from sharing
a device.  It cannot reserve a GPU against unrelated software, so clusters
should enforce their normal scheduler allocation in addition to this gate.

Examples (from the repository root)::

    python scripts/run_classifier_matrix.py --discover-only
    python scripts/run_classifier_matrix.py -- python -m classifier_experiment.run_matrix
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence, TextIO


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from classifier_experiment.environment import artifact_root  # noqa: E402


SCHEMA_VERSION = 1
MIN_FREE_MEMORY_MIB = 20_000
MAX_GPU_UTILIZATION_PERCENT = 0
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_MAX_CHECKS = 10


class InventoryError(RuntimeError):
    """The accelerator inventory could not be established safely."""


class NoEligibleGpuError(RuntimeError):
    """No GPU met the frozen admission rule after the configured checks."""


@dataclass(frozen=True)
class GpuRecord:
    index: int
    uuid: str
    name: str
    memory_total_mib: int
    memory_free_mib: int
    utilization_gpu_percent: int
    active_compute_processes: tuple[dict[str, Any], ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_query(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    try:
        completed = runner(
            list(command), check=True, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InventoryError(f"nvidia-smi query failed: {type(exc).__name__}: {exc}") from exc
    return completed.stdout


def _csv_rows(output: str, expected_columns: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if not row or all(not value.strip() for value in row):
            continue
        values = [value.strip() for value in row]
        if len(values) != expected_columns:
            raise InventoryError(f"unexpected nvidia-smi CSV row: {row!r}")
        rows.append(values)
    return rows


def query_gpu_inventory(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    executable: str | None = None,
) -> list[GpuRecord]:
    """Return a verified physical-GPU and active-compute-process inventory.

    Both queries are mandatory.  In particular, a failed process query is not
    interpreted as an empty process list.
    """

    smi = executable or shutil.which("nvidia-smi")
    if smi is None:
        raise InventoryError("nvidia-smi not found on PATH")

    gpu_output = _run_query(
        [
            smi,
            "--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        runner=runner,
    )
    process_output = _run_query(
        [
            smi,
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        runner=runner,
    )

    processes: dict[str, list[dict[str, Any]]] = {}
    for uuid, pid_text, process_name, used_text in _csv_rows(process_output, 4):
        try:
            pid = int(pid_text)
            used_mib = int(used_text)
        except ValueError as exc:
            raise InventoryError("unparseable compute-process inventory") from exc
        processes.setdefault(uuid, []).append(
            {"pid": pid, "process_name": process_name, "used_memory_mib": used_mib}
        )

    records: list[GpuRecord] = []
    seen_indices: set[int] = set()
    seen_uuids: set[str] = set()
    for index_text, uuid, name, total_text, free_text, utilization_text in _csv_rows(
        gpu_output, 6
    ):
        try:
            index = int(index_text)
            total = int(total_text)
            free = int(free_text)
            utilization = int(utilization_text)
        except ValueError as exc:
            raise InventoryError("unparseable GPU inventory") from exc
        if index < 0 or total <= 0 or not 0 <= free <= total or not 0 <= utilization <= 100:
            raise InventoryError("out-of-range value in GPU inventory")
        if not uuid or index in seen_indices or uuid in seen_uuids:
            raise InventoryError("missing or duplicate GPU identity")
        seen_indices.add(index)
        seen_uuids.add(uuid)
        records.append(
            GpuRecord(
                index=index,
                uuid=uuid,
                name=name,
                memory_total_mib=total,
                memory_free_mib=free,
                utilization_gpu_percent=utilization,
                active_compute_processes=tuple(processes.get(uuid, ())),
            )
        )
    if not records:
        raise InventoryError("nvidia-smi exposed no GPUs")
    unknown_process_gpus = sorted(set(processes).difference(seen_uuids))
    if unknown_process_gpus:
        raise InventoryError("compute process refers to a GPU absent from inventory")
    return sorted(records, key=lambda gpu: gpu.index)


def classify_gpu(
    gpu: GpuRecord,
    *,
    min_free_memory_mib: int = MIN_FREE_MEMORY_MIB,
) -> dict[str, Any]:
    """Return auditable eligibility and rejection reasons for one GPU."""

    if min_free_memory_mib < MIN_FREE_MEMORY_MIB:
        raise ValueError(
            f"free-memory threshold cannot be below frozen {MIN_FREE_MEMORY_MIB} MiB"
        )
    reasons: list[str] = []
    if gpu.active_compute_processes:
        reasons.append("active_compute_process")
    if gpu.utilization_gpu_percent > MAX_GPU_UTILIZATION_PERCENT:
        reasons.append("nonzero_gpu_utilization")
    if gpu.memory_free_mib < min_free_memory_mib:
        reasons.append("below_free_memory_threshold")
    return {**asdict(gpu), "eligible": not reasons, "rejection_reasons": reasons}


def classify_inventory(
    inventory: Sequence[GpuRecord],
    *,
    min_free_memory_mib: int = MIN_FREE_MEMORY_MIB,
) -> list[dict[str, Any]]:
    return [
        classify_gpu(gpu, min_free_memory_mib=min_free_memory_mib)
        for gpu in inventory
    ]


def select_idle_gpu(classified: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Select deterministically from eligible GPUs; never relax eligibility."""

    eligible = [gpu for gpu in classified if gpu.get("eligible") is True]
    if not eligible:
        return None
    return min(eligible, key=lambda gpu: (-int(gpu["memory_free_mib"]), int(gpu["index"])))


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _inventory_evidence(
    *, checks: Sequence[Mapping[str, Any]], min_free_memory_mib: int
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "recorded_at_utc": _utc_now(),
        "admission_rule": {
            "minimum_free_memory_mib": min_free_memory_mib,
            "maximum_gpu_utilization_percent": MAX_GPU_UTILIZATION_PERCENT,
            "active_compute_processes_allowed": False,
            "inventory_query_failures_are_eligible": False,
        },
        "checks": list(checks),
    }


def discover_once(
    *,
    artifacts_dir: Path,
    min_free_memory_mib: int = MIN_FREE_MEMORY_MIB,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    executable: str | None = None,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    """Write the required discovery snapshot and return its eligible candidate."""

    try:
        inventory = query_gpu_inventory(runner=runner, executable=executable)
        classified = classify_inventory(
            inventory, min_free_memory_mib=min_free_memory_mib
        )
        candidate = select_idle_gpu(classified)
        check: dict[str, Any] = {
            "checked_at_utc": _utc_now(),
            "query_succeeded": True,
            "error": None,
            "gpus": classified,
            "eligible_candidate": None
            if candidate is None
            else {"index": candidate["index"], "uuid": candidate["uuid"]},
        }
    except InventoryError as exc:
        candidate = None
        check = {
            "checked_at_utc": _utc_now(),
            "query_succeeded": False,
            "error": f"{type(exc).__name__}: {exc}",
            "gpus": [],
            "eligible_candidate": None,
        }
    evidence = _inventory_evidence(
        checks=[check], min_free_memory_mib=min_free_memory_mib
    )
    _atomic_write_json(artifacts_dir / "preflight" / "gpu_inventory.json", evidence)
    return check, candidate


@dataclass
class GpuClaim:
    gpu: Mapping[str, Any]
    lock_stream: TextIO
    acquired_at_utc: str

    def release(self) -> None:
        if not self.lock_stream.closed:
            fcntl.flock(self.lock_stream.fileno(), fcntl.LOCK_UN)
            self.lock_stream.close()

    def __enter__(self) -> "GpuClaim":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _try_claim(
    candidate: Mapping[str, Any], *, artifacts_dir: Path
) -> GpuClaim | None:
    lock_dir = artifacts_dir / "preflight" / "gpu_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe_uuid = "".join(
        character if character.isalnum() or character in "-." else "_"
        for character in str(candidate["uuid"])
    )
    stream = (lock_dir / f"{safe_uuid}.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return None
    return GpuClaim(gpu=candidate, lock_stream=stream, acquired_at_utc=_utc_now())


def wait_for_idle_gpu(
    *,
    artifacts_dir: Path,
    min_free_memory_mib: int = MIN_FREE_MEMORY_MIB,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_checks: int = DEFAULT_MAX_CHECKS,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    executable: str | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> GpuClaim:
    """Poll, lock, and revalidate an eligible device without weakening rules."""

    if max_checks < 1:
        raise ValueError("max_checks must be at least one")
    if poll_interval_seconds < 0:
        raise ValueError("poll interval cannot be negative")
    checks: list[dict[str, Any]] = []
    evidence_path = artifacts_dir / "preflight" / "gpu_inventory.json"

    for check_number in range(1, max_checks + 1):
        try:
            classified = classify_inventory(
                query_gpu_inventory(runner=runner, executable=executable),
                min_free_memory_mib=min_free_memory_mib,
            )
            check: dict[str, Any] = {
                "check_number": check_number,
                "checked_at_utc": _utc_now(),
                "query_succeeded": True,
                "error": None,
                "gpus": classified,
                "eligible_candidate": None,
            }
            for candidate in sorted(
                (gpu for gpu in classified if gpu["eligible"]),
                key=lambda gpu: (-gpu["memory_free_mib"], gpu["index"]),
            ):
                claim = _try_claim(candidate, artifacts_dir=artifacts_dir)
                if claim is None:
                    continue
                # Close the observation/claim race for cooperating launchers and
                # detect unrelated work that appeared while the lock was taken.
                try:
                    reclassified = classify_inventory(
                        query_gpu_inventory(runner=runner, executable=executable),
                        min_free_memory_mib=min_free_memory_mib,
                    )
                except BaseException:
                    claim.release()
                    raise
                verified = next(
                    (gpu for gpu in reclassified if gpu["uuid"] == candidate["uuid"]),
                    None,
                )
                if verified is not None and verified["eligible"]:
                    claim.gpu = verified
                    check["eligible_candidate"] = {
                        "index": verified["index"], "uuid": verified["uuid"]
                    }
                    check["claim_acquired"] = True
                    checks.append(check)
                    _atomic_write_json(
                        evidence_path,
                        _inventory_evidence(
                            checks=checks, min_free_memory_mib=min_free_memory_mib
                        ),
                    )
                    return claim
                claim.release()
            check["claim_acquired"] = False
        except InventoryError as exc:
            check = {
                "check_number": check_number,
                "checked_at_utc": _utc_now(),
                "query_succeeded": False,
                "error": f"{type(exc).__name__}: {exc}",
                "gpus": [],
                "eligible_candidate": None,
                "claim_acquired": False,
            }
        checks.append(check)
        _atomic_write_json(
            evidence_path,
            _inventory_evidence(checks=checks, min_free_memory_mib=min_free_memory_mib),
        )
        if check_number < max_checks:
            sleeper(poll_interval_seconds)

    raise NoEligibleGpuError(
        f"no idle GPU with at least {min_free_memory_mib} MiB free after {max_checks} checks"
    )


def _allocation_stem(gpu_uuid: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    safe_uuid = "".join(c if c.isalnum() or c in "-." else "_" for c in gpu_uuid)
    return f"{stamp}-pid{os.getpid()}-{safe_uuid}"


def _write_new_allocation(
    artifacts_dir: Path, gpu_uuid: str, evidence: dict[str, Any]
) -> Path:
    """Create allocation evidence exclusively, adding a suffix on collision."""

    directory = artifacts_dir / "allocations"
    directory.mkdir(parents=True, exist_ok=True)
    stem = _allocation_stem(gpu_uuid)
    for suffix in range(10_000):
        unique_stem = stem if suffix == 0 else f"{stem}-{suffix}"
        path = directory / f"{unique_stem}.json"
        evidence["allocation_id"] = unique_stem
        payload = (
            json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n"
        )
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            return path
        except FileExistsError:
            continue
    raise RuntimeError("could not create a unique allocation evidence identity")


def launch_on_claimed_gpu(
    command: Sequence[str],
    *,
    claim: GpuClaim,
    artifacts_dir: Path,
    environ: Mapping[str, str] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    """Run a command with exactly the claimed physical device visible."""

    if not command:
        raise ValueError("a command is required")
    if claim.gpu.get("eligible") is not True:
        raise ValueError("refusing to launch on an ineligible GPU")
    child_environment = dict(os.environ if environ is None else environ)
    gpu_uuid = str(claim.gpu["uuid"])
    child_environment["CUDA_VISIBLE_DEVICES"] = gpu_uuid
    child_environment["NVIDIA_VISIBLE_DEVICES"] = gpu_uuid
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "allocation_id": None,
        "acquired_at_utc": claim.acquired_at_utc,
        "released_at_utc": None,
        "gpu": dict(claim.gpu),
        "command": list(command),
        "environment": {
            "CUDA_VISIBLE_DEVICES": gpu_uuid,
            "NVIDIA_VISIBLE_DEVICES": gpu_uuid,
        },
        "status": "running",
        "returncode": None,
    }
    evidence_path = _write_new_allocation(artifacts_dir, gpu_uuid, evidence)
    try:
        completed = command_runner(list(command), env=child_environment, check=False)
        evidence["returncode"] = int(completed.returncode)
        evidence["status"] = "succeeded" if completed.returncode == 0 else "failed"
        return int(completed.returncode)
    except BaseException as exc:
        evidence["status"] = "launcher_error"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        evidence["released_at_utc"] = _utc_now()
        _atomic_write_json(evidence_path, evidence)
        claim.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, default=None)
    parser.add_argument("--min-free-memory-mib", type=int, default=MIN_FREE_MEMORY_MIB)
    parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--max-checks", type=int, default=DEFAULT_MAX_CHECKS)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    artifacts_dir = (args.artifacts_dir or artifact_root()).resolve()

    if args.discover_only:
        if args.command:
            parser.error("--discover-only does not accept a command")
        check, candidate = discover_once(
            artifacts_dir=artifacts_dir,
            min_free_memory_mib=args.min_free_memory_mib,
        )
        print(artifacts_dir / "preflight" / "gpu_inventory.json")
        if not check["query_succeeded"]:
            return 2
        return 0 if candidate is not None else 3

    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("provide --discover-only or a command after --")
    try:
        claim = wait_for_idle_gpu(
            artifacts_dir=artifacts_dir,
            min_free_memory_mib=args.min_free_memory_mib,
            poll_interval_seconds=args.poll_interval_seconds,
            max_checks=args.max_checks,
        )
    except (NoEligibleGpuError, ValueError) as exc:
        parser.exit(4, f"GPU allocation stopped: {exc}\n")
    return launch_on_claimed_gpu(command, claim=claim, artifacts_dir=artifacts_dir)


if __name__ == "__main__":
    raise SystemExit(main())
