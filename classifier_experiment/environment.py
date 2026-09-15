"""Record the isolated classifier experiment's software and accelerator state.

The module intentionally depends only on the Python standard library. This
lets it produce useful preflight evidence even when an ML package or CUDA is
missing. Run it from the repository root after installing
``ml-requirements.lock``::

    python -m classifier_experiment.environment

By default, evidence is written to the artifact directory assigned to this
experiment run. ``CLASSIFIER_EXPERIMENT_ARTIFACTS_DIR`` may point at a
different run artifact root for an intentional reproduction or for tests.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPOSITORY_ROOT / "ml-requirements.lock"
DEFAULT_ARTIFACTS_DIR = Path(
    "/export/ssd/gate/users/xingyi/goose/dummy_agent/codex_langgraph_agent/"
    ".agent/sessions/i-want-to-compare-if-6c86a2/artifacts"
)
ARTIFACTS_ENV_VAR = "CLASSIFIER_EXPERIMENT_ARTIFACTS_DIR"
CUDA_VISIBILITY_VARIABLES = (
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
)


def read_pins(lock_path: Path = LOCK_PATH) -> dict[str, str]:
    """Read exact, unconditional ``name==version`` pins from the ML lock."""

    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        lock_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1 or any(marker in line for marker in (";", "[", "]")):
            raise ValueError(
                f"{lock_path}:{line_number}: expected an unconditional exact pin"
            )
        name, version = (part.strip() for part in line.split("==", maxsplit=1))
        if not name or not version:
            raise ValueError(f"{lock_path}:{line_number}: incomplete package pin")
        normalized_name = name.lower().replace("_", "-")
        if normalized_name in pins:
            raise ValueError(f"{lock_path}:{line_number}: duplicate pin for {name}")
        pins[normalized_name] = version
    if not pins:
        raise ValueError(f"{lock_path} contains no package pins")
    return pins


def _package_record(name: str, pinned_version: str) -> dict[str, Any]:
    try:
        installed_version: str | None = metadata.version(name)
    except metadata.PackageNotFoundError:
        installed_version = None
    return {
        "pinned_version": pinned_version,
        "installed_version": installed_version,
        "matches_pin": installed_version is not None
        and _public_version(installed_version) == pinned_version,
    }


def _public_version(version: str) -> str:
    """Ignore a wheel's PEP 440 local suffix (for example ``+cu121``)."""

    return version.split("+", maxsplit=1)[0]


def _run_nvidia_smi() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    executable = shutil.which("nvidia-smi")
    status: dict[str, Any] = {
        "executable": executable,
        "query_succeeded": False,
        "error": None,
    }
    if executable is None:
        status["error"] = "nvidia-smi not found on PATH"
        return status, []

    fields = (
        "index,name,uuid,driver_version,memory.total,memory.free,"
        "compute_cap"
    )
    command = [
        executable,
        f"--query-gpu={fields}",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        return status, []

    keys = (
        "index",
        "name",
        "uuid",
        "driver_version",
        "memory_total_mib",
        "memory_free_mib",
        "compute_capability",
    )
    gpus: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        values = [value.strip() for value in line.split(",")]
        if len(values) != len(keys):
            status["error"] = f"unexpected nvidia-smi row: {line!r}"
            return status, []
        gpu = dict(zip(keys, values))
        for numeric_key in ("index", "memory_total_mib", "memory_free_mib"):
            try:
                gpu[numeric_key] = int(gpu[numeric_key])
            except ValueError:
                pass
        gpus.append(gpu)
    status["query_succeeded"] = True
    return status, gpus


def _torch_record() -> dict[str, Any]:
    record: dict[str, Any] = {
        "import_succeeded": False,
        "error": None,
        "version": None,
        "cuda_runtime_version": None,
        "cudnn_version": None,
        "cuda_available": False,
        "visible_device_count": 0,
        "visible_devices": [],
    }
    try:
        import torch
    except Exception as exc:  # CUDA loader failures are also evidence.
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record

    record.update(
        {
            "import_succeeded": True,
            "version": torch.__version__,
            "cuda_runtime_version": torch.version.cuda,
            "cudnn_version": torch.backends.cudnn.version(),
            "cuda_available": torch.cuda.is_available(),
            "visible_device_count": torch.cuda.device_count(),
        }
    )
    devices: list[dict[str, Any]] = []
    for index in range(torch.cuda.device_count()):
        try:
            properties = torch.cuda.get_device_properties(index)
            devices.append(
                {
                    "logical_index": index,
                    "name": properties.name,
                    "total_memory_bytes": properties.total_memory,
                    "compute_capability": f"{properties.major}.{properties.minor}",
                }
            )
        except Exception as exc:
            devices.append(
                {"logical_index": index, "error": f"{type(exc).__name__}: {exc}"}
            )
    record["visible_devices"] = devices
    return record


def build_environment_record(
    *,
    lock_path: Path = LOCK_PATH,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Collect a JSON-serializable environment record without writing it."""

    environment = os.environ if environ is None else environ
    pins = read_pins(lock_path)
    smi_status, physical_gpus = _run_nvidia_smi()
    return {
        "schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "lock_file": {
            "path": str(lock_path.resolve()),
            "packages": {
                name: _package_record(name, version)
                for name, version in sorted(pins.items())
            },
        },
        "python": {
            "version": platform.python_version(),
            "version_detail": sys.version,
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "cuda": {
            "visibility_environment": {
                name: {
                    "is_set": name in environment,
                    "value": environment.get(name),
                }
                for name in CUDA_VISIBILITY_VARIABLES
            },
            "torch": _torch_record(),
            "nvidia_smi": smi_status,
        },
        "gpus": physical_gpus,
    }


def artifact_root(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    configured = environment.get(ARTIFACTS_ENV_VAR)
    return Path(configured).expanduser().resolve() if configured else DEFAULT_ARTIFACTS_DIR


def write_environment_record(
    record: Mapping[str, Any], *, artifacts_dir: Path | None = None
) -> Path:
    """Atomically write ``preflight/environment.json`` below an artifact root."""

    root = (artifacts_dir or artifact_root()).resolve()
    output = root / "preflight" / "environment.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=LOCK_PATH,
        help="exact ML lock to inspect (default: repository ml-requirements.lock)",
    )
    args = parser.parse_args(argv)
    record = build_environment_record(lock_path=args.lock_file)
    output = write_environment_record(record)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
