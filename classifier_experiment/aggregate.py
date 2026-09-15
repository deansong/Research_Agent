"""Strict aggregation for the preregistered 12-cell classifier matrix.

The aggregation is deliberately fail-closed.  Every logical matrix cell must
be represented, failed attempts remain in the output, and a statistic is only
reported when all members of its preregistered group are successful.  Thus a
consumer cannot mistake a mean over two seeds for the frozen three-seed result.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
from statistics import mean, stdev
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import ArtifactValidationError, validate_run_artifacts


SCHEMA_VERSION = 1
DATASETS = ("fancyzhx/ag_news", "stanfordnlp/imdb")
CHECKPOINTS = {
    "base": "Qwen/Qwen2.5-1.5B",
    "instruct": "Qwen/Qwen2.5-1.5B-Instruct",
}
SEEDS = (42, 43, 44)
METRICS = ("accuracy", "macro_f1")


class AggregationError(ValueError):
    """Inputs do not represent the frozen experiment matrix."""


def expected_cells() -> tuple[tuple[str, str, int], ...]:
    """Return ``(dataset, checkpoint_role, seed)`` in canonical order."""

    return tuple(
        (dataset, role, seed)
        for dataset in DATASETS
        for role in CHECKPOINTS
        for seed in SEEDS
    )


def _finite_probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AggregationError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise AggregationError(f"{label} must be finite and in [0, 1]")
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AggregationError(f"{label} must be a positive integer")
    return value


def _role(record: Mapping[str, Any]) -> str:
    role = record.get("checkpoint_role")
    checkpoint = record.get("checkpoint")
    if role is None:
        matches = [name for name, repo_id in CHECKPOINTS.items() if checkpoint == repo_id]
        if len(matches) != 1:
            raise AggregationError(f"unknown checkpoint {checkpoint!r}")
        role = matches[0]
    if role not in CHECKPOINTS:
        raise AggregationError(f"unknown checkpoint_role {role!r}")
    if checkpoint is not None and checkpoint != CHECKPOINTS[role]:
        raise AggregationError(
            f"checkpoint {checkpoint!r} does not match role {role!r}"
        )
    return str(role)


def _normalise_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise AggregationError("each run record must be an object")
    dataset = record.get("dataset")
    if dataset not in DATASETS:
        raise AggregationError(f"unknown dataset {dataset!r}")
    seed = record.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed not in SEEDS:
        raise AggregationError(f"seed must be one of {SEEDS}")
    role = _role(record)
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AggregationError("run_id must be a non-empty string")
    attempt = _positive_int(record.get("attempt", 1), "attempt")
    status = record.get("status")
    if status not in {"complete", "failed", "incomplete"}:
        raise AggregationError(f"invalid run status {status!r}")

    result = {
        "run_id": run_id,
        "dataset": dataset,
        "checkpoint_role": role,
        "checkpoint": CHECKPOINTS[role],
        "seed": seed,
        "attempt": attempt,
        "status": status,
        "reason": record.get("reason"),
        "run_dir": record.get("run_dir"),
        "accuracy": None,
        "macro_f1": None,
        "num_examples": None,
    }
    if status == "complete":
        for metric in METRICS:
            result[metric] = _finite_probability(record.get(metric), metric)
        result["num_examples"] = _positive_int(
            record.get("num_examples"), "num_examples"
        )
    return result


def read_run_record(run_dir: Path | str) -> dict[str, Any]:
    """Read one identity-bound run directory into an aggregation record."""

    directory = Path(run_dir)
    try:
        manifest = validate_run_artifacts(directory, require_complete=False)
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    except (ArtifactValidationError, OSError, json.JSONDecodeError) as exc:
        raise AggregationError(f"invalid run directory {directory}: {exc}") from exc
    identity = manifest["identity"]
    record: dict[str, Any] = {
        **identity,
        "status": manifest["status"],
        "reason": metadata.get("reason"),
        "run_dir": str(directory.resolve()),
    }
    if manifest["status"] == "complete":
        try:
            metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # defensive; validator read it first
            raise AggregationError(f"cannot read metrics for {directory}: {exc}") from exc
        record.update({name: metrics[name] for name in (*METRICS, "num_examples")})
    return _normalise_record(record)


def discover_run_directories(runs_dir: Path | str) -> list[Path]:
    """Discover direct child run directories without ignoring malformed runs."""

    root = Path(runs_dir)
    if not root.is_dir():
        raise AggregationError(f"runs directory does not exist: {root}")
    directories = sorted(path for path in root.iterdir() if path.is_dir())
    if not directories:
        raise AggregationError(f"runs directory is empty: {root}")
    return directories


def _group_statistic(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row["status"] == "complete"]
    complete = len(successful) == len(rows)
    result: dict[str, Any] = {
        "complete": complete,
        "expected_runs": len(rows),
        "successful_runs": len(successful),
    }
    for metric in METRICS:
        # Never emit partial statistics.  Sample SD requires at least two
        # observations; frozen groups always contain three when complete.
        values = [float(row[metric]) for row in successful]
        result[f"{metric}_mean"] = mean(values) if complete else None
        result[f"{metric}_sample_std"] = stdev(values) if complete else None
    return result


def aggregate_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate attempts into the exact twelve logical matrix cells.

    The highest-numbered attempt is the cell outcome.  Earlier attempts remain
    in ``attempts`` as retry evidence.  Attempt numbers must be unique within a
    cell.  Missing or out-of-protocol cells raise :class:`AggregationError`.
    """

    attempts = [_normalise_record(record) for record in records]
    if not attempts:
        raise AggregationError("no run records supplied")
    run_ids = [record["run_id"] for record in attempts]
    if len(set(run_ids)) != len(run_ids):
        raise AggregationError("run_id values must be globally unique")

    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for record in attempts:
        key = (record["dataset"], record["checkpoint_role"], record["seed"])
        grouped.setdefault(key, []).append(record)
    expected = set(expected_cells())
    observed = set(grouped)
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing or extra:
        raise AggregationError(
            f"run matrix must contain exactly 12 logical cells; missing={missing}, extra={extra}"
        )

    per_seed: list[dict[str, Any]] = []
    sorted_attempts: list[dict[str, Any]] = []
    for key in expected_cells():
        cell_attempts = sorted(grouped[key], key=lambda row: row["attempt"])
        numbers = [row["attempt"] for row in cell_attempts]
        if len(numbers) != len(set(numbers)):
            raise AggregationError(f"duplicate attempt number for cell {key}")
        if numbers != list(range(1, numbers[-1] + 1)):
            raise AggregationError(f"non-contiguous retry attempts for cell {key}: {numbers}")
        sorted_attempts.extend(cell_attempts)
        selected = cell_attempts[-1]
        per_seed.append(
            {
                "dataset": key[0],
                "checkpoint_role": key[1],
                "checkpoint": CHECKPOINTS[key[1]],
                "seed": key[2],
                "status": selected["status"],
                "reason": selected["reason"],
                "selected_run_id": selected["run_id"],
                "selected_attempt": selected["attempt"],
                "attempt_count": len(cell_attempts),
                "accuracy": selected["accuracy"],
                "macro_f1": selected["macro_f1"],
                "num_examples": selected["num_examples"],
            }
        )

    combinations: list[dict[str, Any]] = []
    for dataset in DATASETS:
        for role in CHECKPOINTS:
            rows = [
                row for row in per_seed
                if row["dataset"] == dataset and row["checkpoint_role"] == role
            ]
            combinations.append(
                {"dataset": dataset, "checkpoint_role": role, **_group_statistic(rows)}
            )

    paired: list[dict[str, Any]] = []
    for dataset in DATASETS:
        for seed in SEEDS:
            by_role = {
                row["checkpoint_role"]: row
                for row in per_seed
                if row["dataset"] == dataset and row["seed"] == seed
            }
            complete = all(by_role[role]["status"] == "complete" for role in CHECKPOINTS)
            paired.append(
                {
                    "dataset": dataset,
                    "seed": seed,
                    "complete": complete,
                    "base_run_id": by_role["base"]["selected_run_id"],
                    "instruct_run_id": by_role["instruct"]["selected_run_id"],
                    "accuracy_difference": (
                        by_role["instruct"]["accuracy"] - by_role["base"]["accuracy"]
                        if complete else None
                    ),
                    "macro_f1_difference": (
                        by_role["instruct"]["macro_f1"] - by_role["base"]["macro_f1"]
                        if complete else None
                    ),
                }
            )

    paired_statistics: list[dict[str, Any]] = []
    for dataset in DATASETS:
        rows = [row for row in paired if row["dataset"] == dataset]
        complete = all(row["complete"] for row in rows)
        item: dict[str, Any] = {
            "dataset": dataset,
            "complete": complete,
            "expected_pairs": len(SEEDS),
            "successful_pairs": sum(bool(row["complete"]) for row in rows),
        }
        for metric in METRICS:
            values = [
                float(row[f"{metric}_difference"])
                for row in rows if row["complete"]
            ]
            item[f"{metric}_difference_mean"] = mean(values) if complete else None
            item[f"{metric}_difference_sample_std"] = stdev(values) if complete else None
        paired_statistics.append(item)

    return {
        "schema_version": SCHEMA_VERSION,
        "matrix": {
            "datasets": list(DATASETS),
            "checkpoints": dict(CHECKPOINTS),
            "seeds": list(SEEDS),
            "expected_cells": 12,
            "represented_cells": len(per_seed),
            "all_cells_successful": all(row["status"] == "complete" for row in per_seed),
        },
        "attempts": sorted_attempts,
        "per_seed": per_seed,
        "combination_statistics": combinations,
        "paired_differences": paired,
        "paired_statistics": paired_statistics,
    }


def aggregate_run_directories(run_dirs: Iterable[Path | str]) -> dict[str, Any]:
    return aggregate_records(read_run_record(path) for path in run_dirs)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise AggregationError(f"cannot export empty table {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    if any(list(row) != fields for row in rows):
        raise AggregationError(f"table {path.name} has inconsistent columns")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def export_tables(result: Mapping[str, Any], output_dir: Path | str) -> list[Path]:
    """Export canonical JSON plus each machine-readable table as JSON and CSV."""

    destination = Path(output_dir)
    tables = (
        "attempts",
        "per_seed",
        "combination_statistics",
        "paired_differences",
        "paired_statistics",
    )
    written: list[Path] = []
    aggregate_path = destination / "aggregate.json"
    _atomic_json(aggregate_path, result)
    written.append(aggregate_path)
    for name in tables:
        rows = result.get(name)
        if not isinstance(rows, list):
            raise AggregationError(f"aggregate result is missing table {name!r}")
        json_path = destination / f"{name}.json"
        csv_path = destination / f"{name}.csv"
        _atomic_json(json_path, rows)
        _atomic_csv(csv_path, rows)
        written.extend((json_path, csv_path))
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    result = aggregate_run_directories(discover_run_directories(arguments.runs_dir))
    export_tables(result, arguments.output_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
