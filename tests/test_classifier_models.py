from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment import models


class FakeTokenizer:
    eos_token = "<|endoftext|>"
    eos_token_id = 151643
    pad_token = "<|endoftext|>"
    pad_token_id = 151643
    padding_side = "left"

    def apply_chat_template(self, *args, **kwargs):
        raise AssertionError("the model factory must not use a chat template")


@pytest.fixture
def base_factory_calls():
    calls = {"tokenizer": [], "model": [], "collator": []}

    def tokenizer_loader(repo_id, **kwargs):
        calls["tokenizer"].append((repo_id, kwargs))
        return FakeTokenizer()

    def model_loader(repo_id, **kwargs):
        calls["model"].append((repo_id, kwargs))
        return SimpleNamespace(config=SimpleNamespace())

    def collator_factory(**kwargs):
        calls["collator"].append(kwargs)
        return SimpleNamespace(**kwargs)

    return calls, tokenizer_loader, model_loader, collator_factory


@pytest.mark.parametrize(
    ("dataset_id", "num_labels", "max_length"),
    [
        ("fancyzhx/ag_news", 4, 256),
        ("stanfordnlp/imdb", 2, 512),
    ],
)
def test_base_factory_uses_pinned_pair_and_matching_heads(
    base_factory_calls, dataset_id, num_labels, max_length
):
    calls, tokenizer_loader, model_loader, collator_factory = base_factory_calls
    built = models.build_sequence_classifier(
        dataset_id,
        tokenizer_loader=tokenizer_loader,
        model_loader=model_loader,
        collator_factory=collator_factory,
    )

    expected_revision = "8faed761d45a263340a0528343f099c05c9a4323"
    assert calls["tokenizer"] == [
        (
            "Qwen/Qwen2.5-1.5B",
            {
                "revision": expected_revision,
                "trust_remote_code": False,
                "use_fast": True,
            },
        )
    ]
    model_id, model_kwargs = calls["model"][0]
    assert model_id == "Qwen/Qwen2.5-1.5B"
    assert model_kwargs["revision"] == expected_revision
    assert model_kwargs["num_labels"] == num_labels
    assert set(model_kwargs["id2label"]) == set(range(num_labels))
    assert set(model_kwargs["label2id"].values()) == set(range(num_labels))
    assert model_kwargs["pad_token_id"] == 151643
    assert built.model.config.num_labels == num_labels
    assert built.model.config.pad_token_id == 151643
    assert built.tokenizer.padding_side == "right"
    assert calls["collator"] == [
        {"tokenizer": built.tokenizer, "padding": "longest"}
    ]
    assert built.dataset["max_length"] == max_length
    assert built.checkpoint["tokenizer"]["use_chat_template"] is False


def test_base_config_rejects_moving_revision(tmp_path):
    config = json.loads(models.BASE_CONFIG_PATH.read_text(encoding="utf-8"))
    config["model"]["revision"] = "main"
    path = tmp_path / "moving.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable commit SHA"):
        models.load_checkpoint_config(path)


def test_base_preflight_records_architecture_tokenizer_padding_and_heads(tmp_path):
    output = models.write_base_preflight(artifacts_dir=tmp_path)
    assert output == tmp_path / "preflight" / "base_model.json"
    evidence = json.loads(output.read_text(encoding="utf-8"))

    assert evidence["model"] == {
        "repo_id": "Qwen/Qwen2.5-1.5B",
        "resolved_commit_sha": "8faed761d45a263340a0528343f099c05c9a4323",
        "architecture_family": "Qwen2.5-1.5B",
        "transformers_model_type": "qwen2",
        "source_architecture": "Qwen2ForCausalLM",
        "sequence_classification_class": "Qwen2ForSequenceClassification",
    }
    assert evidence["tokenizer"]["paired_with_model"] is True
    assert evidence["tokenizer"]["raw_text"] is True
    assert evidence["tokenizer"]["chat_template_used"] is False
    assert evidence["tokenizer"]["padding"] == {
        "strategy": "longest",
        "side": "right",
        "pad_token": "<|endoftext|>",
        "pad_token_id": 151643,
        "same_as_eos": True,
    }
    assert evidence["classifier_output_sizes"] == {
        "fancyzhx/ag_news": 4,
        "stanfordnlp/imdb": 2,
    }


def test_base_factory_falls_back_to_eos_for_missing_pad_token(base_factory_calls):
    calls, _, model_loader, collator_factory = base_factory_calls

    class MissingPadTokenizer(FakeTokenizer):
        pad_token = None
        pad_token_id = None

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "pad_token" and value == self.eos_token:
                object.__setattr__(self, "pad_token_id", self.eos_token_id)

    built = models.build_sequence_classifier(
        "stanfordnlp/imdb",
        tokenizer_loader=lambda *args, **kwargs: MissingPadTokenizer(),
        model_loader=model_loader,
        collator_factory=collator_factory,
    )
    assert built.tokenizer.pad_token == built.tokenizer.eos_token
    assert built.tokenizer.pad_token_id == built.tokenizer.eos_token_id == 151643


@pytest.mark.parametrize(
    ("dataset_id", "num_labels"),
    [("fancyzhx/ag_news", 4), ("stanfordnlp/imdb", 2)],
)
def test_instruct_uses_same_factory_with_pinned_pair_and_matching_heads(
    base_factory_calls, dataset_id, num_labels
):
    calls, tokenizer_loader, model_loader, collator_factory = base_factory_calls
    built = models.build_sequence_classifier(
        dataset_id,
        config_path=models.INSTRUCT_CONFIG_PATH,
        tokenizer_loader=tokenizer_loader,
        model_loader=model_loader,
        collator_factory=collator_factory,
    )

    revision = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
    assert calls["tokenizer"] == [
        (
            "Qwen/Qwen2.5-1.5B-Instruct",
            {"revision": revision, "trust_remote_code": False, "use_fast": True},
        )
    ]
    model_id, kwargs = calls["model"][0]
    assert model_id == "Qwen/Qwen2.5-1.5B-Instruct"
    assert kwargs["revision"] == revision
    assert kwargs["num_labels"] == num_labels
    assert kwargs["pad_token_id"] == 151643
    assert built.model.config.num_labels == num_labels
    assert built.tokenizer.padding_side == "right"
    assert built.checkpoint["tokenizer"]["use_chat_template"] is False


def test_instruct_preflight_records_pinned_raw_text_comparator(tmp_path):
    output = models.write_instruct_preflight(artifacts_dir=tmp_path)
    assert output == tmp_path / "preflight" / "instruct_model.json"
    evidence = json.loads(output.read_text(encoding="utf-8"))

    assert evidence["checkpoint_role"] == "instruct"
    assert evidence["model"]["resolved_commit_sha"] == (
        "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
    )
    assert evidence["model"]["architecture_family"] == "Qwen2.5-1.5B"
    assert evidence["tokenizer"]["paired_with_model"] is True
    assert evidence["tokenizer"]["raw_text"] is True
    assert evidence["tokenizer"]["chat_template_used"] is False
    assert evidence["tokenizer"]["padding"] == {
        "strategy": "longest",
        "side": "right",
        "pad_token": "<|endoftext|>",
        "pad_token_id": 151643,
        "same_as_eos": True,
    }
    assert evidence["classifier_output_sizes"] == {
        "fancyzhx/ag_news": 4,
        "stanfordnlp/imdb": 2,
    }


def test_instruct_non_checkpoint_behavior_matches_base():
    base = models.load_checkpoint_config(models.BASE_CONFIG_PATH)
    instruct = models.load_checkpoint_config(models.INSTRUCT_CONFIG_PATH)

    assert instruct["datasets"] == base["datasets"]
    assert instruct["tokenizer"]["padding"] == base["tokenizer"]["padding"]
    assert instruct["tokenizer"]["use_fast"] == base["tokenizer"]["use_fast"]
    assert instruct["tokenizer"]["use_chat_template"] is False
    for field in (
        "architecture_family",
        "transformers_model_type",
        "source_architecture",
        "sequence_classification_class",
        "trust_remote_code",
    ):
        assert instruct["model"][field] == base["model"][field]
