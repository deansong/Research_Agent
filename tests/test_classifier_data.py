from __future__ import annotations

from collections import Counter
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment.data import (
    load_classification_dataset,
    tokenize_prepared_dataset,
)


FIXTURE_REVISION = "0123456789abcdef0123456789abcdef01234567"


class FixtureSplit(list):
    def __init__(self, rows, fingerprint):
        super().__init__(rows)
        self._fingerprint = fingerprint

    def select(self, indices):
        return FixtureSplit([self[index] for index in indices], self._fingerprint + "-selected")


def make_rows(class_sizes, prefix):
    rows = []
    for label, size in enumerate(class_sizes):
        rows.extend(
            {"text": f"{prefix}-{label}-{offset}", "label": label}
            for offset in range(size)
        )
    return rows


@pytest.fixture(params=[
    ("fancyzhx/ag_news", [20, 30, 40, 50], [4, 4, 4, 4]),
    ("stanfordnlp/imdb", [55, 45], [10, 10]),
])
def dataset_case(request):
    dataset_id, train_sizes, test_sizes = request.param
    raw = {
        "train": FixtureSplit(make_rows(train_sizes, "train"), "official-train-fp"),
        "test": FixtureSplit(make_rows(test_sizes, "test"), "official-test-fp"),
    }
    if dataset_id == "stanfordnlp/imdb":
        raw["unsupervised"] = FixtureSplit(
            [{"text": "must-not-be-read", "label": -1}], "unlabeled-fp"
        )
    calls = []

    def loader(name, *, revision):
        calls.append((name, revision))
        return raw

    return dataset_id, train_sizes, test_sizes, raw, calls, loader


def test_split_domains_counts_proportions_and_official_test(dataset_case):
    dataset_id, train_sizes, test_sizes, raw, calls, loader = dataset_case
    prepared = load_classification_dataset(
        dataset_id, revision=FIXTURE_REVISION, loader=loader
    )
    manifest = prepared.manifest

    assert calls == [(dataset_id, FIXTURE_REVISION)]
    assert prepared.test is raw["test"]
    assert manifest.label_domain == tuple(range(len(train_sizes)))
    assert manifest.split_sizes == {
        "train": int(sum(train_sizes) * 0.9),
        "validation": int(sum(train_sizes) * 0.1),
        "test": sum(test_sizes),
    }
    assert len(prepared.validation) == pytest.approx(len(raw["train"]) * 0.1)

    for split_name, split in (
        ("train", prepared.train),
        ("validation", prepared.validation),
        ("test", prepared.test),
    ):
        actual = Counter(str(row["label"]) for row in split)
        assert manifest.class_counts[split_name] == {
            str(label): actual[str(label)] for label in manifest.label_domain
        }
        assert set(actual) == {str(label) for label in manifest.label_domain}

    train_text = {row["text"] for row in prepared.train}
    validation_text = {row["text"] for row in prepared.validation}
    test_text = {row["text"] for row in prepared.test}
    assert train_text.isdisjoint(validation_text)
    assert train_text.isdisjoint(test_text)
    assert validation_text.isdisjoint(test_text)
    assert train_text | validation_text == {row["text"] for row in raw["train"]}
    assert manifest.source_fingerprints == {
        "train": "official-train-fp",
        "test": "official-test-fp",
    }
    assert all(len(value) == 64 for value in manifest.content_fingerprints.values())


def test_splits_and_hashes_are_reproducible_and_seed_sensitive(dataset_case):
    dataset_id, _, _, _, _, loader = dataset_case
    first = load_classification_dataset(dataset_id, revision=FIXTURE_REVISION, loader=loader)
    repeated = load_classification_dataset(dataset_id, revision=FIXTURE_REVISION, loader=loader)
    another_seed = load_classification_dataset(
        dataset_id, revision=FIXTURE_REVISION, seed=43, loader=loader
    )

    assert first.manifest.split_index_hashes == repeated.manifest.split_index_hashes
    assert [row["text"] for row in first.train] == [row["text"] for row in repeated.train]
    assert first.manifest.split_index_hashes != another_seed.manifest.split_index_hashes


def test_imdb_unlabeled_split_is_excluded():
    class ForbiddenSplit:
        def __len__(self):
            raise AssertionError("IMDb unlabeled split was inspected")

    raw = {
        "train": FixtureSplit(make_rows([10, 10], "train"), "train-fp"),
        "test": FixtureSplit(make_rows([3, 3], "test"), "test-fp"),
        "unsupervised": ForbiddenSplit(),
    }
    prepared = load_classification_dataset(
        "stanfordnlp/imdb",
        revision=FIXTURE_REVISION,
        loader=lambda *args, **kwargs: raw,
    )

    all_text = [row["text"] for split in (prepared.train, prepared.validation, prepared.test)
                for row in split]
    assert "must-not-be-read" not in all_text
    assert set(prepared.manifest.class_counts) == {"train", "validation", "test"}


def test_tokenization_uses_raw_text_without_chat_template(dataset_case):
    dataset_id, _, _, _, _, loader = dataset_case
    prepared = load_classification_dataset(dataset_id, revision=FIXTURE_REVISION, loader=loader)

    class RecordingTokenizer:
        def __init__(self):
            self.calls = []

        def __call__(self, texts, **kwargs):
            self.calls.append((texts, kwargs))
            return {
                "input_ids": [[len(text)] for text in texts],
                "attention_mask": [[1] for _ in texts],
            }

        def apply_chat_template(self, *args, **kwargs):
            raise AssertionError("chat templates are forbidden")

    tokenizer = RecordingTokenizer()
    tokenized = tokenize_prepared_dataset(prepared, tokenizer, max_length=256)

    assert set(tokenized) == {"train", "validation", "test"}
    assert len(tokenizer.calls) == 3
    assert all(
        kwargs == {"truncation": True, "max_length": 256, "padding": False}
        for _, kwargs in tokenizer.calls
    )
    assert all("labels" in row and "input_ids" in row for row in tokenized["train"])


def test_bad_label_domain_is_rejected():
    raw = {
        "train": FixtureSplit(make_rows([10, 10], "train"), "train-fp"),
        "test": FixtureSplit([{"text": "unlabeled", "label": -1}], "test-fp"),
    }
    with pytest.raises(ValueError, match="label domain"):
        load_classification_dataset(
            "stanfordnlp/imdb",
            revision=FIXTURE_REVISION,
            loader=lambda *args, **kwargs: raw,
        )


@pytest.mark.parametrize(
    "moving_or_incomplete_revision",
    [
        "main",
        "refs/heads/main",
        "v1.0.0",
        "refs/tags/v1.0.0",
        "0123456",
        "0123456789abcdef0123456789abcdef0123456",
        "0123456789ABCDEF0123456789ABCDEF01234567",
    ],
)
def test_moving_tags_branches_and_abbreviated_revisions_are_rejected(
    moving_or_incomplete_revision,
):
    loader_called = False

    def loader(*args, **kwargs):
        nonlocal loader_called
        loader_called = True
        raise AssertionError("invalid revision reached the dataset loader")

    with pytest.raises(ValueError, match="full immutable commit SHA"):
        load_classification_dataset(
            "stanfordnlp/imdb",
            revision=moving_or_incomplete_revision,
            loader=loader,
        )
    assert loader_called is False
