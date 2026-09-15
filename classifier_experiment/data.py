"""Reproducible data preparation for the classifier comparison.

The functions in this module deliberately do not know which Qwen checkpoint is
being evaluated.  A prepared dataset (and its manifest) can therefore be
shared by the matched base and instruction runs without either model changing
the examples it sees.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    text_column: str
    label_column: str
    labels: tuple[int, ...]


DATASET_SPECS: dict[str, DatasetSpec] = {
    "fancyzhx/ag_news": DatasetSpec(
        dataset_id="fancyzhx/ag_news",
        text_column="text",
        label_column="label",
        labels=(0, 1, 2, 3),
    ),
    "stanfordnlp/imdb": DatasetSpec(
        dataset_id="stanfordnlp/imdb",
        text_column="text",
        label_column="label",
        labels=(0, 1),
    ),
}


@dataclass(frozen=True)
class DatasetManifest:
    """Machine-readable provenance for one deterministic preparation."""

    dataset_id: str
    revision: str
    seed: int
    validation_fraction: float
    text_column: str
    label_column: str
    label_domain: tuple[int, ...]
    split_sizes: dict[str, int]
    class_counts: dict[str, dict[str, int]]
    source_fingerprints: dict[str, str]
    content_fingerprints: dict[str, str]
    split_index_hashes: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreparedDataset:
    train: Any
    validation: Any
    test: Any
    manifest: DatasetManifest


def _default_loader(dataset_id: str, *, revision: str) -> Mapping[str, Any]:
    # Keep the heavyweight optional dependency out of module import and unit
    # tests.  Production environments receive it from the experiment lock.
    from datasets import load_dataset

    return load_dataset(dataset_id, revision=revision)


def _rows(split: Any):
    for index in range(len(split)):
        yield index, split[index]


def _select(split: Any, indices: Sequence[int]) -> Any:
    if hasattr(split, "select"):
        return split.select(list(indices))
    return [split[index] for index in indices]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _index_hash(indices: Sequence[int]) -> str:
    """Hash an ordered index list using an unambiguous representation."""

    return _canonical_hash(list(indices))


def _content_fingerprint(split: Any, spec: DatasetSpec) -> str:
    digest = sha256()
    for index, row in _rows(split):
        item = [index, row[spec.text_column], int(row[spec.label_column])]
        digest.update(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _source_fingerprint(split: Any, spec: DatasetSpec) -> str:
    fingerprint = getattr(split, "_fingerprint", None)
    if fingerprint:
        return str(fingerprint)
    # Controlled fixtures and other sequence implementations do not have a
    # datasets fingerprint. Their content hash is an equally stable identity.
    return _content_fingerprint(split, spec)


def _labels(split: Any, label_column: str) -> list[int]:
    return [int(row[label_column]) for _, row in _rows(split)]


def _validate_label_domain(
    train_labels: Sequence[int], test_labels: Sequence[int], spec: DatasetSpec
) -> None:
    expected = set(spec.labels)
    for split_name, values in (("train", train_labels), ("test", test_labels)):
        observed = set(values)
        if observed != expected:
            raise ValueError(
                f"{spec.dataset_id} {split_name} label domain {sorted(observed)} "
                f"does not match expected {sorted(expected)}"
            )


def stratified_split_indices(
    labels: Sequence[int], *, validation_fraction: float = 0.1, seed: int = 42
) -> tuple[list[int], list[int]]:
    """Return deterministic train/validation indices with exact total size.

    Per-class quotas use largest-remainder allocation. Selection within each
    class is ordered by SHA-256 rather than a library PRNG, making the result
    stable across Python, NumPy, scikit-learn, and datasets versions.
    """

    if not 0.0 < validation_fraction < 1.0:
        raise ValueError("validation_fraction must be strictly between 0 and 1")
    if not labels:
        raise ValueError("cannot split an empty training set")

    groups: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(int(label), []).append(index)

    validation_size = int(math.floor(len(labels) * validation_fraction + 0.5))
    if validation_size <= 0 or validation_size >= len(labels):
        raise ValueError("validation fraction produces an empty split")

    exact = {label: len(indices) * validation_fraction for label, indices in groups.items()}
    quotas = {label: int(math.floor(value)) for label, value in exact.items()}
    remainder = validation_size - sum(quotas.values())
    order = sorted(groups, key=lambda label: (-(exact[label] - quotas[label]), label))
    for label in order[:remainder]:
        quotas[label] += 1

    validation_set: set[int] = set()
    for label, indices in groups.items():
        ranked = sorted(
            indices,
            key=lambda index: sha256(f"{seed}:{label}:{index}".encode()).digest(),
        )
        validation_set.update(ranked[: quotas[label]])

    train_indices = [index for index in range(len(labels)) if index not in validation_set]
    validation_indices = sorted(validation_set)
    return train_indices, validation_indices


def _counts(labels: Sequence[int], domain: Sequence[int]) -> dict[str, int]:
    counts = Counter(int(label) for label in labels)
    return {str(label): counts[label] for label in domain}


def load_classification_dataset(
    dataset_id: str,
    *,
    revision: str,
    seed: int = 42,
    validation_fraction: float = 0.1,
    loader: Callable[..., Mapping[str, Any]] | None = None,
) -> PreparedDataset:
    """Load and prepare one pinned official classification dataset.

    Only ``train`` and ``test`` are read. In particular, the IMDb
    ``unsupervised`` split is neither inspected nor returned. The official
    test object is preserved verbatim and is never involved in split choices.
    """

    if dataset_id not in DATASET_SPECS:
        raise ValueError(f"unsupported dataset: {dataset_id!r}")
    if not revision or not revision.strip():
        raise ValueError("an explicit dataset revision is required")

    spec = DATASET_SPECS[dataset_id]
    raw = (loader or _default_loader)(dataset_id, revision=revision)
    missing = {"train", "test"}.difference(raw)
    if missing:
        raise ValueError(f"{dataset_id} is missing official splits: {sorted(missing)}")

    official_train = raw["train"]
    official_test = raw["test"]
    train_labels = _labels(official_train, spec.label_column)
    test_labels = _labels(official_test, spec.label_column)
    _validate_label_domain(train_labels, test_labels, spec)

    train_indices, validation_indices = stratified_split_indices(
        train_labels, validation_fraction=validation_fraction, seed=seed
    )
    prepared_train = _select(official_train, train_indices)
    validation = _select(official_train, validation_indices)

    split_labels = {
        "train": [train_labels[index] for index in train_indices],
        "validation": [train_labels[index] for index in validation_indices],
        "test": test_labels,
    }
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        revision=revision,
        seed=seed,
        validation_fraction=validation_fraction,
        text_column=spec.text_column,
        label_column=spec.label_column,
        label_domain=spec.labels,
        split_sizes={name: len(values) for name, values in split_labels.items()},
        class_counts={
            name: _counts(values, spec.labels) for name, values in split_labels.items()
        },
        source_fingerprints={
            "train": _source_fingerprint(official_train, spec),
            "test": _source_fingerprint(official_test, spec),
        },
        content_fingerprints={
            "official_train": _content_fingerprint(official_train, spec),
            "train": _content_fingerprint(prepared_train, spec),
            "validation": _content_fingerprint(validation, spec),
            "test": _content_fingerprint(official_test, spec),
        },
        split_index_hashes={
            "train": _index_hash(train_indices),
            "validation": _index_hash(validation_indices),
        },
    )
    return PreparedDataset(prepared_train, validation, official_test, manifest)


def tokenize_split(
    split: Any,
    tokenizer: Any,
    *,
    text_column: str,
    label_column: str = "label",
    max_length: int,
) -> Any:
    """Tokenize raw classification text, without prompts or chat templates.

    No fixed-length padding is done here; the shared training collator pads
    each batch consistently for both checkpoints.
    """

    if max_length <= 0:
        raise ValueError("max_length must be positive")

    def encode(batch: Mapping[str, Sequence[Any]]) -> dict[str, Any]:
        encoded = dict(
            tokenizer(
                list(batch[text_column]),
                truncation=True,
                max_length=max_length,
                padding=False,
            )
        )
        encoded["labels"] = [int(label) for label in batch[label_column]]
        return encoded

    if hasattr(split, "map"):
        columns = list(getattr(split, "column_names"))
        return split.map(encode, batched=True, remove_columns=columns)

    batch = {
        text_column: [row[text_column] for row in split],
        label_column: [row[label_column] for row in split],
    }
    encoded = encode(batch)
    return [
        {key: values[index] for key, values in encoded.items()}
        for index in range(len(split))
    ]


def tokenize_prepared_dataset(
    prepared: PreparedDataset, tokenizer: Any, *, max_length: int
) -> dict[str, Any]:
    """Apply the same raw-text tokenizer path to all three prepared splits."""

    manifest = prepared.manifest
    kwargs = {
        "text_column": manifest.text_column,
        "label_column": manifest.label_column,
        "max_length": max_length,
    }
    return {
        "train": tokenize_split(prepared.train, tokenizer, **kwargs),
        "validation": tokenize_split(prepared.validation, tokenizer, **kwargs),
        "test": tokenize_split(prepared.test, tokenizer, **kwargs),
    }

