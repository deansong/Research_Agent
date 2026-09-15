"""Metrics and durable per-run artifacts for the classifier experiment.

The artifact writer deliberately separates *creating* a run from *opening* one.
Creation is exclusive, so a retry must use a new directory and identity.  Once
created, individual JSON documents are replaced atomically, but only through a
writer whose identity matches the immutable identity stored on disk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from numbers import Integral
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
REQUIRED_FILES = (
    "config.json",
    "metadata.json",
    "history.json",
    "metrics.json",
    "trainable_parameters.json",
    "selected_checkpoint.json",
)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ArtifactValidationError(ValueError):
    """An artifact does not satisfy the experiment's on-disk schema."""


class RunIdentityError(RuntimeError):
    """A path is already owned by another immutable run identity."""


@dataclass(frozen=True)
class RunIdentity:
    """Fields that distinguish a matrix cell and each retry of that cell."""

    run_id: str
    dataset: str
    checkpoint: str
    seed: int
    attempt: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not _RUN_ID_RE.fullmatch(self.run_id):
            raise ArtifactValidationError(
                "run_id must be 1-128 portable filename characters"
            )
        if (
            not isinstance(self.dataset, str)
            or not self.dataset.strip()
            or not isinstance(self.checkpoint, str)
            or not self.checkpoint.strip()
        ):
            raise ArtifactValidationError("dataset and checkpoint must be non-empty")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ArtifactValidationError("seed must be an integer")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ArtifactValidationError("attempt must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _identity(value: RunIdentity | Mapping[str, Any]) -> RunIdentity:
    if isinstance(value, RunIdentity):
        return value
    try:
        return RunIdentity(
            run_id=value["run_id"],
            dataset=value["dataset"],
            checkpoint=value["checkpoint"],
            seed=value["seed"],
            attempt=value.get("attempt", 1),
        )
    except KeyError as exc:
        raise ArtifactValidationError(f"run identity is missing {exc.args[0]!r}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value, allow_nan=False, indent=2, sort_keys=True, ensure_ascii=False
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"value is not strict JSON: {exc}") from exc
    return (rendered + "\n").encode("utf-8")


def atomic_write_json(path: Path | str, value: Any) -> None:
    """Atomically replace one JSON file, including file and directory fsyncs."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(value)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not permit fsync on a directory.  The file
            # replacement is still atomic on all supported local platforms.
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _integer_labels(values: Sequence[int], name: str) -> tuple[int, ...]:
    result: list[int] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
            raise ValueError(f"{name}[{index}] must be a non-negative integer")
        result.append(int(value))
    return tuple(result)


def accuracy(predictions: Sequence[int], labels: Sequence[int]) -> float:
    """Return exact classification accuracy for non-empty matched inputs."""

    predicted = _integer_labels(predictions, "predictions")
    expected = _integer_labels(labels, "labels")
    if len(predicted) != len(expected):
        raise ValueError("predictions and labels must have the same length")
    if not expected:
        raise ValueError("metrics require at least one example")
    return sum(left == right for left, right in zip(predicted, expected)) / len(expected)


def macro_f1(
    predictions: Sequence[int], labels: Sequence[int], *, num_labels: int | None = None
) -> float:
    """Return the unweighted mean of per-class F1 scores.

    Supplying ``num_labels`` includes official classes absent from a fixture or
    shard, assigning them F1=0.  Comparative runs should always supply it.
    """

    predicted = _integer_labels(predictions, "predictions")
    expected = _integer_labels(labels, "labels")
    if len(predicted) != len(expected):
        raise ValueError("predictions and labels must have the same length")
    if not expected:
        raise ValueError("metrics require at least one example")
    if num_labels is None:
        classes = tuple(sorted(set(predicted).union(expected)))
    else:
        if isinstance(num_labels, bool) or not isinstance(num_labels, int) or num_labels < 1:
            raise ValueError("num_labels must be a positive integer")
        if any(value >= num_labels for value in predicted + expected):
            raise ValueError("a prediction or label is outside num_labels")
        classes = tuple(range(num_labels))

    scores: list[float] = []
    for label in classes:
        true_positive = sum(p == label and y == label for p, y in zip(predicted, expected))
        false_positive = sum(p == label and y != label for p, y in zip(predicted, expected))
        false_negative = sum(p != label and y == label for p, y in zip(predicted, expected))
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(0.0 if denominator == 0 else 2 * true_positive / denominator)
    return sum(scores) / len(scores)


def classification_metrics(
    predictions: Sequence[int], labels: Sequence[int], *, num_labels: int | None = None
) -> dict[str, Any]:
    """Build the canonical official-test metric payload."""

    return {
        "accuracy": accuracy(predictions, labels),
        "macro_f1": macro_f1(predictions, labels, num_labels=num_labels),
        "num_examples": len(labels),
    }


# Familiar name for evaluation callbacks.
compute_metrics = classification_metrics


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArtifactValidationError(f"{label} must be a JSON object")
    _json_bytes(value)
    return value


def _metric_value(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactValidationError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ArtifactValidationError(f"{name} must be finite and in [0, 1]")
    return number


def validate_metrics(value: Mapping[str, Any]) -> None:
    record = _require_mapping(value, "metrics")
    _metric_value(record.get("accuracy"), "accuracy")
    _metric_value(record.get("macro_f1"), "macro_f1")
    count = record.get("num_examples")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ArtifactValidationError("num_examples must be a positive integer")


def validate_history(value: Sequence[Mapping[str, Any]]) -> None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ArtifactValidationError("history must be an array")
    previous = 0
    for item in value:
        record = _require_mapping(item, "history entry")
        epoch = record.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= previous:
            raise ArtifactValidationError("history epochs must be strictly increasing positive integers")
        previous = epoch
        for key in ("mean_train_loss", "validation_accuracy"):
            metric = record.get(key)
            if isinstance(metric, bool) or not isinstance(metric, (int, float)) or not math.isfinite(metric):
                raise ArtifactValidationError(f"history {key} must be finite and numeric")
        _metric_value(record["validation_accuracy"], "validation_accuracy")


def validate_trainable_parameters(value: Mapping[str, Any]) -> None:
    record = _require_mapping(value, "trainable parameters")
    for key in ("trainable_parameters", "total_parameters"):
        count = record.get(key)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ArtifactValidationError(f"{key} must be a non-negative integer")
    if record["trainable_parameters"] > record["total_parameters"]:
        raise ArtifactValidationError("trainable_parameters exceeds total_parameters")
    names = record.get("trainable_names")
    if (
        isinstance(names, (str, bytes))
        or not isinstance(names, Sequence)
        or not all(isinstance(name, str) for name in names)
    ):
        raise ArtifactValidationError("trainable_names must be an array of strings")


def validate_selected_checkpoint(value: Mapping[str, Any]) -> None:
    record = _require_mapping(value, "selected checkpoint")
    epoch = record.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ArtifactValidationError("selected checkpoint epoch must be positive")
    if not isinstance(record.get("path"), str) or not record["path"]:
        raise ArtifactValidationError("selected checkpoint path must be non-empty")
    _metric_value(record.get("validation_accuracy"), "validation_accuracy")
    if record.get("tie_break") != "earliest_epoch":
        raise ArtifactValidationError("checkpoint tie_break must be earliest_epoch")


class RunArtifacts:
    """Identity-bound writer for one run directory."""

    def __init__(self, run_dir: Path | str, identity: RunIdentity):
        self.run_dir = Path(run_dir)
        self.identity = identity

    @classmethod
    def create(
        cls,
        run_dir: Path | str,
        *,
        identity: RunIdentity | Mapping[str, Any],
        config: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> "RunArtifacts":
        """Exclusively create a run and its six required JSON documents."""

        parsed = _identity(identity)
        checked_config = dict(_require_mapping(config, "config"))
        extra = dict(_require_mapping(metadata or {}, "metadata"))
        forbidden = {"schema_version", "identity", "status", "created_at_utc", "updated_at_utc"}
        overlap = forbidden.intersection(extra)
        if overlap:
            raise ArtifactValidationError(f"reserved metadata keys: {sorted(overlap)}")
        destination = Path(run_dir)
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            existing = _read_existing_identity(destination)
            if existing is not None and existing != parsed:
                raise RunIdentityError(
                    f"refusing to overwrite different run identity at {destination}"
                )
            raise FileExistsError(f"refusing to overwrite existing run: {destination}")

        now = _utc_now()
        documents = {
            "config.json": checked_config,
            "metadata.json": {
                "schema_version": SCHEMA_VERSION,
                "identity": parsed.to_dict(),
                "status": "incomplete",
                "created_at_utc": now,
                "updated_at_utc": now,
                "reason": "run created; required stages not yet complete",
                **extra,
            },
            "history.json": {"schema_version": SCHEMA_VERSION, "epochs": []},
            "metrics.json": {"schema_version": SCHEMA_VERSION, "status": "not_evaluated"},
            "trainable_parameters.json": {"schema_version": SCHEMA_VERSION, "status": "not_recorded"},
            "selected_checkpoint.json": {"schema_version": SCHEMA_VERSION, "status": "not_selected"},
        }
        writer = cls(destination, parsed)
        try:
            # Claim the identity first.  If a later filesystem error interrupts
            # initialization, the partial directory still identifies its owner.
            for filename in ("metadata.json",) + tuple(
                name for name in REQUIRED_FILES if name != "metadata.json"
            ):
                atomic_write_json(destination / filename, documents[filename])
        except Exception:
            # Keep the exclusively claimed directory as durable incomplete
            # evidence.  A retry must receive a new identity/directory.
            raise
        return writer

    @classmethod
    def open(
        cls, run_dir: Path | str, *, identity: RunIdentity | Mapping[str, Any]
    ) -> "RunArtifacts":
        parsed = _identity(identity)
        stored = _read_existing_identity(Path(run_dir))
        if stored is None:
            raise ArtifactValidationError("run metadata or identity is missing")
        if stored != parsed:
            raise RunIdentityError("refusing to open a different run identity")
        return cls(run_dir, parsed)

    def _assert_identity(self) -> dict[str, Any]:
        metadata = _read_json(self.run_dir / "metadata.json")
        try:
            stored = _identity(metadata["identity"])
        except (KeyError, TypeError) as exc:
            raise ArtifactValidationError("run metadata identity is invalid") from exc
        if stored != self.identity:
            raise RunIdentityError("run identity changed on disk; refusing write")
        return metadata

    def _write(self, filename: str, value: Mapping[str, Any]) -> None:
        self._assert_identity()
        atomic_write_json(self.run_dir / filename, value)

    def write_history(self, epochs: Sequence[Mapping[str, Any]]) -> None:
        validate_history(epochs)
        self._write("history.json", {"schema_version": SCHEMA_VERSION, "epochs": list(epochs)})

    def write_metrics(self, metrics: Mapping[str, Any], *, split: str = "test") -> None:
        if split != "test":
            raise ArtifactValidationError("final metrics split must be the official test split")
        validate_metrics(metrics)
        self._write(
            "metrics.json",
            {**dict(metrics), "schema_version": SCHEMA_VERSION, "status": "evaluated", "split": split},
        )

    def write_trainable_parameters(self, parameters: Mapping[str, Any]) -> None:
        validate_trainable_parameters(parameters)
        self._write(
            "trainable_parameters.json",
            {**dict(parameters), "schema_version": SCHEMA_VERSION, "status": "recorded"},
        )

    def write_selected_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        validate_selected_checkpoint(checkpoint)
        self._write(
            "selected_checkpoint.json",
            {**dict(checkpoint), "schema_version": SCHEMA_VERSION, "status": "selected"},
        )

    def _set_status(self, status: str, *, reason: str | None) -> None:
        metadata = self._assert_identity()
        if metadata.get("status") == "complete" and status != "complete":
            raise ArtifactValidationError("a complete run cannot be made incomplete or failed")
        metadata["status"] = status
        metadata["updated_at_utc"] = _utc_now()
        metadata["reason"] = reason
        atomic_write_json(self.run_dir / "metadata.json", metadata)

    def mark_incomplete(self, reason: str) -> None:
        if not reason:
            raise ArtifactValidationError("incomplete reason must be non-empty")
        self._set_status("incomplete", reason=reason)

    def mark_failed(self, reason: str) -> None:
        if not reason:
            raise ArtifactValidationError("failure reason must be non-empty")
        self._set_status("failed", reason=reason)

    def mark_complete(self) -> None:
        validate_run_artifacts(self.run_dir, expected_identity=self.identity, require_complete=False)
        for filename, expected in (
            ("metrics.json", "evaluated"),
            ("trainable_parameters.json", "recorded"),
            ("selected_checkpoint.json", "selected"),
        ):
            if _read_json(self.run_dir / filename).get("status") != expected:
                raise ArtifactValidationError(f"cannot complete run: {filename} is not {expected}")
        self._set_status("complete", reason=None)


# Alternate descriptive spelling retained for callers that prefer "writer".
RunArtifactWriter = RunArtifacts


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"cannot read valid JSON from {path}: {exc}") from exc
    return dict(_require_mapping(value, path.name))


def _read_existing_identity(run_dir: Path) -> RunIdentity | None:
    path = run_dir / "metadata.json"
    if not path.is_file():
        return None
    try:
        return _identity(_read_json(path)["identity"])
    except (ArtifactValidationError, KeyError, TypeError):
        return None


def validate_run_artifacts(
    run_dir: Path | str,
    *,
    expected_identity: RunIdentity | Mapping[str, Any] | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Validate required files and return a compact manifest."""

    directory = Path(run_dir)
    missing = [name for name in REQUIRED_FILES if not (directory / name).is_file()]
    if missing:
        raise ArtifactValidationError(f"missing required run artifacts: {missing}")
    config = _read_json(directory / "config.json")
    metadata = _read_json(directory / "metadata.json")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactValidationError("unsupported metadata schema_version")
    stored = _identity(metadata.get("identity", {}))
    if expected_identity is not None and stored != _identity(expected_identity):
        raise RunIdentityError("run artifact identity does not match expected identity")
    if metadata.get("status") not in {"incomplete", "failed", "complete"}:
        raise ArtifactValidationError("invalid run status")
    if require_complete and metadata["status"] != "complete":
        raise ArtifactValidationError(f"run is {metadata['status']}, not complete")

    history = _read_json(directory / "history.json")
    if history.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactValidationError("unsupported history schema_version")
    validate_history(history.get("epochs"))
    metrics = _read_json(directory / "metrics.json")
    trainable = _read_json(directory / "trainable_parameters.json")
    selected = _read_json(directory / "selected_checkpoint.json")
    for document, label, statuses in (
        (metrics, "metrics", {"not_evaluated", "evaluated"}),
        (trainable, "trainable_parameters", {"not_recorded", "recorded"}),
        (selected, "selected_checkpoint", {"not_selected", "selected"}),
    ):
        if document.get("schema_version") != SCHEMA_VERSION:
            raise ArtifactValidationError(f"unsupported {label} schema_version")
        if document.get("status") not in statuses:
            raise ArtifactValidationError(f"invalid {label} status")
    if metrics.get("status") == "evaluated":
        validate_metrics(metrics)
        if metrics.get("split") != "test":
            raise ArtifactValidationError("evaluated metrics must use official test split")
    if trainable.get("status") == "recorded":
        validate_trainable_parameters(trainable)
    if selected.get("status") == "selected":
        validate_selected_checkpoint(selected)
    if metadata["status"] == "complete":
        if not history["epochs"]:
            raise ArtifactValidationError("complete run has empty training history")
        expected_statuses = (
            (metrics, "evaluated"),
            (trainable, "recorded"),
            (selected, "selected"),
        )
        if any(document.get("status") != status for document, status in expected_statuses):
            raise ArtifactValidationError("complete run has unfinished required artifacts")
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": stored.to_dict(),
        "status": metadata["status"],
        "files": list(REQUIRED_FILES),
        "config": config,
    }


def create_run_artifacts(
    artifacts_root: Path | str,
    *,
    identity: RunIdentity | Mapping[str, Any],
    config: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> RunArtifacts:
    """Create ``<artifacts_root>/runs/<run_id>`` without collision fallback."""

    parsed = _identity(identity)
    return RunArtifacts.create(
        Path(artifacts_root) / "runs" / parsed.run_id,
        identity=parsed,
        config=config,
        metadata=metadata,
    )
