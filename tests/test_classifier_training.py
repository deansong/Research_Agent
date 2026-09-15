from __future__ import annotations

import pathlib
from types import SimpleNamespace
import sys

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("peft")
transformers = pytest.importorskip("transformers")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from classifier_experiment import train


class TinyAttention(torch.nn.Module):
    def __init__(self, width: int):
        super().__init__()
        self.q_proj = torch.nn.Linear(width, width)
        self.k_proj = torch.nn.Linear(width, width)
        self.v_proj = torch.nn.Linear(width, width)
        self.o_proj = torch.nn.Linear(width, width)

    def forward(self, values):
        combined = self.q_proj(values) + self.k_proj(values) + self.v_proj(values)
        return torch.tanh(self.o_proj(combined))


class TinyClassifier(torch.nn.Module):
    def __init__(self, width: int = 8):
        super().__init__()
        self.config = transformers.PretrainedConfig(tie_word_embeddings=False)
        self.embed = torch.nn.Embedding(24, width)
        self.attention = TinyAttention(width)
        self.score = torch.nn.Linear(width, 2, bias=False)

    def prepare_inputs_for_generation(self, *args, **kwargs):
        return kwargs

    def forward(self, input_ids, labels=None, **kwargs):
        hidden = self.attention(self.embed(input_ids)).mean(dim=1)
        logits = self.score(hidden)
        loss = None if labels is None else torch.nn.functional.cross_entropy(logits, labels)
        return SimpleNamespace(loss=loss, logits=logits)


def batches():
    return [
        {
            "input_ids": torch.tensor([[1, 2, 3], [4, 5, 6]]),
            "labels": torch.tensor([0, 1]),
        },
        {
            "input_ids": torch.tensor([[7, 8, 9], [10, 11, 12]]),
            "labels": torch.tensor([1, 0]),
        },
    ]


def test_lora_configuration_and_trainable_boundary():
    adapted = train.apply_lora(TinyClassifier())
    boundary = train.assert_trainable_boundary(adapted)

    assert train.FROZEN_PROTOCOL.lora_targets == ("q_proj", "k_proj", "v_proj", "o_proj")
    assert train.FROZEN_PROTOCOL.lora_rank == 16
    assert train.FROZEN_PROTOCOL.lora_alpha == 32
    assert train.FROZEN_PROTOCOL.lora_dropout == 0.05
    assert boundary.trainable_names
    assert all("lora_" in name or ".score." in name for name in boundary.trainable_names)
    assert any("lora_A" in name for name in boundary.trainable_names)
    assert any("score" in name for name in boundary.trainable_names)
    assert not adapted.base_model.model.embed.weight.requires_grad


def test_boundary_rejects_unfrozen_backbone():
    adapted = train.apply_lora(TinyClassifier())
    adapted.base_model.model.embed.weight.requires_grad = True
    with pytest.raises(RuntimeError, match="boundary violation"):
        train.assert_trainable_boundary(adapted)


def test_optimizer_scheduler_and_precision_fallback():
    adapted = train.apply_lora(TinyClassifier())
    optimizer, scheduler, warmup = train.build_optimizer_and_scheduler(
        adapted, optimizer_steps=10
    )

    assert isinstance(optimizer, torch.optim.AdamW)
    assert optimizer.defaults["lr"] == pytest.approx(2e-4)
    assert optimizer.defaults["weight_decay"] == pytest.approx(0.01)
    assert warmup == 1
    assert scheduler is not None
    assert train.select_precision("cpu").name == "fp32"


def test_synthetic_optimization_changes_only_adapters_and_head(tmp_path):
    adapted = train.apply_lora(TinyClassifier())
    before = {name: parameter.detach().clone() for name, parameter in adapted.named_parameters()}
    protocol = train.TrainingProtocol(
        epochs=1,
        effective_batch_size=4,
        learning_rate=0.02,
    )
    result = train.train_model(
        adapted,
        batches(),
        batches(),
        run_dir=tmp_path / "run",
        seed=42,
        device="cpu",
        micro_batch_size=2,
        protocol=protocol,
    )

    assert all(torch.isfinite(torch.tensor(item["mean_train_loss"])) for item in result.epochs)
    changed = {
        name
        for name, parameter in adapted.named_parameters()
        if not torch.equal(before[name], parameter.detach())
    }
    assert changed
    assert changed.issubset(set(result.trainable_boundary["trainable_names"]))
    assert torch.equal(before["base_model.model.embed.weight"], adapted.base_model.model.embed.weight)
    assert pathlib.Path(result.best_checkpoint).is_dir()


def test_best_checkpoint_uses_earliest_epoch_on_tie(tmp_path, monkeypatch):
    adapted = train.apply_lora(TinyClassifier())
    monkeypatch.setattr(train, "validation_accuracy", lambda *args, **kwargs: 0.75)
    protocol = train.TrainingProtocol(epochs=3, effective_batch_size=4)
    result = train.train_model(
        adapted,
        batches(),
        batches(),
        run_dir=tmp_path / "tie-run",
        seed=42,
        micro_batch_size=2,
        protocol=protocol,
    )

    assert result.best_epoch == 1
    assert result.best_validation_accuracy == 0.75
    assert pathlib.Path(result.best_checkpoint).name == "epoch-001"
    assert not (tmp_path / "tie-run" / "checkpoints" / "epoch-002").exists()


def test_selected_checkpoint_is_restored_after_later_worse_epoch(tmp_path, monkeypatch):
    adapted = train.apply_lora(TinyClassifier())
    scores = iter((1.0, 0.0))
    monkeypatch.setattr(
        train, "validation_accuracy", lambda *args, **kwargs: next(scores)
    )
    protocol = train.TrainingProtocol(epochs=2, effective_batch_size=4)
    result = train.train_model(
        adapted,
        batches(),
        batches(),
        run_dir=tmp_path / "restore-run",
        seed=42,
        micro_batch_size=2,
        protocol=protocol,
    )
    selected = torch.load(
        pathlib.Path(result.best_checkpoint) / "trainable_state.pt",
        map_location="cpu",
        weights_only=True,
    )
    current = dict(adapted.named_parameters())
    assert all(torch.equal(value, current[name].cpu()) for name, value in selected.items())


def test_nonempty_run_is_never_overwritten(tmp_path):
    run_dir = tmp_path / "existing"
    run_dir.mkdir()
    (run_dir / "evidence.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        train.train_model(
            train.apply_lora(TinyClassifier()),
            batches(),
            batches(),
            run_dir=run_dir,
            seed=42,
            micro_batch_size=2,
            protocol=train.TrainingProtocol(epochs=1, effective_batch_size=4),
        )
