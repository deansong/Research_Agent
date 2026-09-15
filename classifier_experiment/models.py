"""Pinned, shared sequence-classification model construction.

The factory is checkpoint-neutral: base and instruction configurations travel
through exactly the same tokenizer, classifier-head, and padding code.  It
never formats classification text or invokes a tokenizer chat template.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from classifier_experiment.environment import artifact_root


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG_PATH = REPOSITORY_ROOT / "configs" / "classifier" / "base.json"
INSTRUCT_CONFIG_PATH = REPOSITORY_ROOT / "configs" / "classifier" / "instruct.json"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ClassifierComponents:
    """Objects and frozen dataset settings returned by the shared factory."""

    model: Any
    tokenizer: Any
    data_collator: Any
    dataset: Mapping[str, Any]
    checkpoint: Mapping[str, Any]


def load_checkpoint_config(path: Path = BASE_CONFIG_PATH) -> dict[str, Any]:
    """Load and strictly validate a pinned classifier checkpoint config."""

    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("unsupported classifier checkpoint schema")

    model = config.get("model", {})
    tokenizer = config.get("tokenizer", {})
    for owner, revision in (
        ("model", model.get("revision")),
        ("tokenizer", tokenizer.get("revision")),
    ):
        if not isinstance(revision, str) or not COMMIT_PATTERN.fullmatch(revision):
            raise ValueError(f"{owner} revision must be a full immutable commit SHA")
    if model.get("repo_id") != tokenizer.get("repo_id"):
        raise ValueError("model and tokenizer must come from the paired repository")
    if model["revision"] != tokenizer["revision"]:
        raise ValueError("model and tokenizer must use the same immutable revision")
    if tokenizer.get("use_chat_template") is not False:
        raise ValueError("classification tokenization must disable chat templates")

    padding = tokenizer.get("padding", {})
    if padding.get("strategy") != "longest" or padding.get("side") not in {
        "left",
        "right",
    }:
        raise ValueError("an explicit dynamic padding strategy and side are required")
    if not isinstance(padding.get("pad_token_id"), int):
        raise ValueError("an explicit integer pad_token_id is required")

    datasets = config.get("datasets", {})
    if not datasets:
        raise ValueError("at least one dataset configuration is required")
    for dataset_id, dataset in datasets.items():
        num_labels = dataset.get("num_labels")
        id2label = dataset.get("id2label", {})
        label2id = dataset.get("label2id", {})
        expected_ids = {str(index) for index in range(num_labels or 0)}
        if set(id2label) != expected_ids or len(label2id) != num_labels:
            raise ValueError(f"{dataset_id} label mapping does not match num_labels")
        if any(label2id.get(label) != int(index) for index, label in id2label.items()):
            raise ValueError(f"{dataset_id} label mappings are not inverses")
    return config


def build_sequence_classifier(
    dataset_id: str,
    *,
    config_path: Path = BASE_CONFIG_PATH,
    torch_dtype: Any | None = None,
    tokenizer_loader: Callable[..., Any] | None = None,
    model_loader: Callable[..., Any] | None = None,
    collator_factory: Callable[..., Any] | None = None,
) -> ClassifierComponents:
    """Build one pinned sequence classifier using the shared experiment path."""

    config = load_checkpoint_config(config_path)
    if dataset_id not in config["datasets"]:
        raise ValueError(f"dataset is not configured: {dataset_id!r}")

    if tokenizer_loader is None or model_loader is None or collator_factory is None:
        from transformers import (
            AutoModelForSequenceClassification,
            AutoTokenizer,
            DataCollatorWithPadding,
        )

        tokenizer_loader = tokenizer_loader or AutoTokenizer.from_pretrained
        model_loader = model_loader or AutoModelForSequenceClassification.from_pretrained
        collator_factory = collator_factory or DataCollatorWithPadding

    model_spec = config["model"]
    tokenizer_spec = config["tokenizer"]
    padding = tokenizer_spec["padding"]
    dataset = config["datasets"][dataset_id]

    tokenizer = tokenizer_loader(
        tokenizer_spec["repo_id"],
        revision=tokenizer_spec["revision"],
        trust_remote_code=model_spec["trust_remote_code"],
        use_fast=tokenizer_spec["use_fast"],
    )
    tokenizer.padding_side = padding["side"]
    if tokenizer.pad_token_id is None:
        if not padding.get("same_as_eos") or tokenizer.eos_token_id is None:
            raise ValueError("tokenizer has no pad token and the EOS fallback is unavailable")
        tokenizer.pad_token = tokenizer.eos_token
    if tokenizer.pad_token_id != padding["pad_token_id"]:
        raise ValueError("resolved tokenizer pad_token_id differs from frozen config")
    if tokenizer.pad_token != padding["pad_token"]:
        raise ValueError("resolved tokenizer pad token differs from frozen config")

    id2label = {int(index): label for index, label in dataset["id2label"].items()}
    model_kwargs = {
        "revision": model_spec["revision"],
        "trust_remote_code": model_spec["trust_remote_code"],
        "num_labels": dataset["num_labels"],
        "id2label": id2label,
        "label2id": dict(dataset["label2id"]),
        "pad_token_id": tokenizer.pad_token_id,
    }
    if torch_dtype is not None:
        model_kwargs["torch_dtype"] = torch_dtype
    model = model_loader(model_spec["repo_id"], **model_kwargs)
    # Be explicit even for Transformers versions which propagate this kwarg.
    model.config.pad_token_id = tokenizer.pad_token_id
    model.config.num_labels = dataset["num_labels"]

    collator = collator_factory(tokenizer=tokenizer, padding=padding["strategy"])
    return ClassifierComponents(
        model=model,
        tokenizer=tokenizer,
        data_collator=collator,
        dataset=dataset,
        checkpoint=config,
    )


def checkpoint_preflight_record(config_path: Path) -> dict[str, Any]:
    """Create auditable checkpoint evidence from a validated config."""

    config = load_checkpoint_config(config_path)
    model = config["model"]
    tokenizer = config["tokenizer"]
    return {
        "schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "checkpoint_role": config["checkpoint_role"],
        "model": {
            "repo_id": model["repo_id"],
            "resolved_commit_sha": model["revision"],
            "architecture_family": model["architecture_family"],
            "transformers_model_type": model["transformers_model_type"],
            "source_architecture": model["source_architecture"],
            "sequence_classification_class": model["sequence_classification_class"],
        },
        "tokenizer": {
            "repo_id": tokenizer["repo_id"],
            "resolved_commit_sha": tokenizer["revision"],
            "tokenizer_class": tokenizer["tokenizer_class"],
            "paired_with_model": (
                tokenizer["repo_id"] == model["repo_id"]
                and tokenizer["revision"] == model["revision"]
            ),
            "raw_text": True,
            "chat_template_used": False,
            "padding": tokenizer["padding"],
        },
        "classifier_output_sizes": {
            dataset_id: settings["num_labels"]
            for dataset_id, settings in config["datasets"].items()
        },
        "label_mappings": {
            dataset_id: {
                "id2label": settings["id2label"],
                "label2id": settings["label2id"],
            }
            for dataset_id, settings in config["datasets"].items()
        },
    }


def base_preflight_record(config_path: Path = BASE_CONFIG_PATH) -> dict[str, Any]:
    """Create auditable base-checkpoint evidence."""

    return checkpoint_preflight_record(config_path)


def instruct_preflight_record(
    config_path: Path = INSTRUCT_CONFIG_PATH,
) -> dict[str, Any]:
    """Create auditable instruction-checkpoint evidence."""

    return checkpoint_preflight_record(config_path)


def write_checkpoint_preflight(
    *, config_path: Path, artifacts_dir: Path | None = None
) -> Path:
    """Write role-named preflight evidence without loading model weights."""

    record = checkpoint_preflight_record(config_path)
    role = record["checkpoint_role"]
    if role not in {"base", "instruct"}:
        raise ValueError(f"unsupported checkpoint role: {role!r}")
    destination = (artifacts_dir or artifact_root()) / "preflight" / f"{role}_model.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def write_base_preflight(
    *, config_path: Path = BASE_CONFIG_PATH, artifacts_dir: Path | None = None
) -> Path:
    return write_checkpoint_preflight(
        config_path=config_path, artifacts_dir=artifacts_dir
    )


def write_instruct_preflight(
    *, config_path: Path = INSTRUCT_CONFIG_PATH, artifacts_dir: Path | None = None
) -> Path:
    return write_checkpoint_preflight(
        config_path=config_path, artifacts_dir=artifacts_dir
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_CONFIG_PATH)
    parser.add_argument("--artifacts-dir", type=Path)
    args = parser.parse_args()
    print(write_checkpoint_preflight(config_path=args.config, artifacts_dir=args.artifacts_dir))


if __name__ == "__main__":
    main()
