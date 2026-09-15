"""Shared LoRA training primitives for the controlled classifier comparison.

This module contains no checkpoint- or dataset-specific tuning.  Both Qwen
initializations pass through these functions with the same frozen protocol.
The official test split is intentionally absent from the training API.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable, Mapping, Sequence


LORA_TARGET_MODULES = ("q_proj", "k_proj", "v_proj", "o_proj")
CLASSIFIER_HEAD_MODULES = ("score",)


@dataclass(frozen=True)
class TrainingProtocol:
    """Preregistered settings shared by every comparative cell."""

    seeds: tuple[int, ...] = (42, 43, 44)
    epochs: int = 3
    learning_rate: float = 2e-4
    weight_decay: float = 0.01
    warmup_ratio: float = 0.10
    effective_batch_size: int = 32
    max_grad_norm: float = 1.0
    lora_rank: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    lora_targets: tuple[str, ...] = LORA_TARGET_MODULES
    classifier_heads: tuple[str, ...] = CLASSIFIER_HEAD_MODULES
    scheduler: str = "linear"
    checkpoint_metric: str = "validation_accuracy"
    checkpoint_tie_break: str = "earliest_epoch"


FROZEN_PROTOCOL = TrainingProtocol()


@dataclass(frozen=True)
class PrecisionPlan:
    name: str
    autocast_device_type: str | None
    autocast_dtype: Any | None
    reason: str


@dataclass(frozen=True)
class TrainableBoundary:
    trainable_names: tuple[str, ...]
    frozen_names: tuple[str, ...]
    trainable_parameters: int
    total_parameters: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrainingResult:
    best_epoch: int
    best_validation_accuracy: float
    best_checkpoint: str
    epochs: tuple[dict[str, Any], ...]
    precision: str
    optimizer_steps: int
    trainable_boundary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def seed_everything(seed: int) -> None:
    """Seed Python and PyTorch and request deterministic kernels."""

    import torch

    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    # warn_only avoids turning an unsupported deterministic CUDA kernel into
    # an undocumented protocol change; the runtime record can retain warnings.
    torch.use_deterministic_algorithms(True, warn_only=True)


def select_precision(device: Any) -> PrecisionPlan:
    """Choose BF16 only when the selected CUDA device safely supports it."""

    import torch

    device = torch.device(device)
    if device.type == "cuda" and torch.cuda.is_available():
        supported = bool(
            getattr(torch.cuda, "is_bf16_supported", lambda: False)()
        )
        if supported:
            return PrecisionPlan(
                "bf16", "cuda", torch.bfloat16, "CUDA device reports BF16 support"
            )
        return PrecisionPlan(
            "fp32",
            None,
            None,
            "safe fallback: selected CUDA device does not report BF16 support",
        )
    return PrecisionPlan(
        "fp32", None, None, "safe fallback: training device is not CUDA"
    )


def apply_lora(
    model: Any,
    *,
    protocol: TrainingProtocol = FROZEN_PROTOCOL,
) -> Any:
    """Freeze ``model`` and attach the preregistered sequence-classifier LoRA.

    Freezing first is deliberate: the subsequent boundary assertion is a
    fail-closed guard against PEFT or architecture changes exposing backbone
    parameters to the optimizer.
    """

    from peft import LoraConfig, TaskType, get_peft_model

    for parameter in model.parameters():
        parameter.requires_grad = False
    config = LoraConfig(
        task_type=TaskType.SEQ_CLS,
        r=protocol.lora_rank,
        lora_alpha=protocol.lora_alpha,
        lora_dropout=protocol.lora_dropout,
        target_modules=list(protocol.lora_targets),
        modules_to_save=list(protocol.classifier_heads),
        bias="none",
    )
    adapted = get_peft_model(model, config)
    assert_trainable_boundary(adapted, classifier_heads=protocol.classifier_heads)
    return adapted


def _is_classifier_parameter(name: str, classifier_heads: Sequence[str]) -> bool:
    parts = name.split(".")
    return any(head in parts for head in classifier_heads)


def assert_trainable_boundary(
    model: Any,
    *,
    classifier_heads: Sequence[str] = CLASSIFIER_HEAD_MODULES,
) -> TrainableBoundary:
    """Require every trainable tensor to belong to LoRA or the classifier.

    The check also requires both categories, preventing a silently frozen head
    or a no-op adapter configuration from passing preflight.
    """

    trainable: list[str] = []
    frozen: list[str] = []
    trainable_count = 0
    total_count = 0
    has_lora = False
    has_head = False
    invalid: list[str] = []
    for name, parameter in model.named_parameters():
        count = parameter.numel()
        total_count += count
        if parameter.requires_grad:
            trainable.append(name)
            trainable_count += count
            is_lora = "lora_" in name
            is_head = _is_classifier_parameter(name, classifier_heads)
            has_lora = has_lora or is_lora
            has_head = has_head or is_head
            if not (is_lora or is_head):
                invalid.append(name)
        else:
            frozen.append(name)
    if invalid:
        raise RuntimeError(
            "trainable-parameter boundary violation: " + ", ".join(invalid)
        )
    if not trainable:
        raise RuntimeError("trainable-parameter boundary is empty")
    if not has_lora:
        raise RuntimeError("no LoRA adapter parameter is trainable")
    if not has_head:
        raise RuntimeError("no classifier-head parameter is trainable")
    return TrainableBoundary(
        tuple(trainable), tuple(frozen), trainable_count, total_count
    )


def build_optimizer_and_scheduler(
    model: Any,
    *,
    optimizer_steps: int,
    protocol: TrainingProtocol = FROZEN_PROTOCOL,
) -> tuple[Any, Any, int]:
    """Build AdamW and the frozen 10%-warmup linear schedule."""

    import torch

    if optimizer_steps <= 0:
        raise ValueError("optimizer_steps must be positive")
    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    if not parameters:
        raise ValueError("optimizer received no trainable parameters")
    optimizer = torch.optim.AdamW(
        parameters,
        lr=protocol.learning_rate,
        weight_decay=protocol.weight_decay,
    )
    warmup_steps = int(math.ceil(optimizer_steps * protocol.warmup_ratio))

    def multiplier(step: int) -> float:
        if warmup_steps and step < warmup_steps:
            return float(step + 1) / float(warmup_steps)
        decay_steps = max(1, optimizer_steps - warmup_steps)
        return max(0.0, float(optimizer_steps - (step + 1)) / decay_steps)

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, multiplier)
    return optimizer, scheduler, warmup_steps


def _model_loss(output: Any) -> Any:
    loss = output.get("loss") if isinstance(output, Mapping) else getattr(output, "loss", None)
    if loss is None:
        raise ValueError("model output does not contain loss")
    return loss


def _model_logits(output: Any) -> Any:
    logits = output.get("logits") if isinstance(output, Mapping) else getattr(output, "logits", None)
    if logits is None:
        raise ValueError("model output does not contain logits")
    return logits


def _move_batch(batch: Mapping[str, Any], device: Any) -> dict[str, Any]:
    return {
        key: value.to(device) if hasattr(value, "to") else value
        for key, value in batch.items()
    }


def validation_accuracy(model: Any, batches: Iterable[Mapping[str, Any]], device: Any) -> float:
    """Compute validation accuracy without mutating model parameters."""

    import torch

    model.eval()
    correct = 0
    examples = 0
    with torch.no_grad():
        for raw_batch in batches:
            batch = _move_batch(raw_batch, device)
            output = model(**batch)
            predictions = _model_logits(output).argmax(dim=-1)
            labels = batch["labels"]
            correct += int((predictions == labels).sum().item())
            examples += int(labels.numel())
    if examples == 0:
        raise ValueError("validation loader is empty")
    return correct / examples


def _save_trainable_checkpoint(
    model: Any, destination: Path, *, epoch: int, validation_accuracy: float
) -> None:
    import torch

    if destination.exists():
        raise FileExistsError(f"refusing to overwrite checkpoint: {destination}")
    destination.mkdir(parents=True)
    state = {
        name: parameter.detach().cpu().clone()
        for name, parameter in model.named_parameters()
        if parameter.requires_grad
    }
    torch.save(state, destination / "trainable_state.pt")
    (destination / "metrics.json").write_text(
        json.dumps(
            {"epoch": epoch, "validation_accuracy": validation_accuracy},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def load_trainable_checkpoint(model: Any, checkpoint: Path) -> None:
    """Restore an adapter/head-only checkpoint into an already adapted model."""

    import torch

    state_path = checkpoint / "trainable_state.pt"
    try:
        state = torch.load(state_path, map_location="cpu", weights_only=True)
    except TypeError:  # pragma: no cover - compatibility with older PyTorch.
        state = torch.load(state_path, map_location="cpu")
    parameters = dict(model.named_parameters())
    expected = {name for name, value in parameters.items() if value.requires_grad}
    if set(state) != expected:
        missing = sorted(expected.difference(state))
        unexpected = sorted(set(state).difference(expected))
        raise RuntimeError(
            f"checkpoint trainable boundary mismatch; missing={missing}, "
            f"unexpected={unexpected}"
        )
    with torch.no_grad():
        for name, value in state.items():
            parameters[name].copy_(value.to(parameters[name].device))


def train_model(
    model: Any,
    train_batches: Any,
    validation_batches: Any,
    *,
    run_dir: Path,
    seed: int,
    device: Any = "cpu",
    micro_batch_size: int,
    protocol: TrainingProtocol = FROZEN_PROTOCOL,
    precision: PrecisionPlan | None = None,
) -> TrainingResult:
    """Optimize one isolated run and select the earliest best epoch.

    ``validation_batches`` must be re-iterable.  A new, empty ``run_dir`` is
    required so a retry cannot overwrite prior evidence.
    """

    import torch

    if seed not in protocol.seeds:
        raise ValueError(f"seed {seed} is outside the frozen seed set")
    if micro_batch_size <= 0 or protocol.effective_batch_size % micro_batch_size:
        raise ValueError("micro batch size must divide effective batch size exactly")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty run: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    seed_everything(seed)
    boundary = assert_trainable_boundary(
        model, classifier_heads=protocol.classifier_heads
    )
    accumulation_steps = protocol.effective_batch_size // micro_batch_size
    batches_per_epoch = len(train_batches)
    steps_per_epoch = math.ceil(batches_per_epoch / accumulation_steps)
    total_steps = steps_per_epoch * protocol.epochs
    optimizer, scheduler, _ = build_optimizer_and_scheduler(
        model, optimizer_steps=total_steps, protocol=protocol
    )
    plan = precision or select_precision(device)
    model.to(device)
    optimizer.zero_grad(set_to_none=True)

    history: list[dict[str, Any]] = []
    best_accuracy = -math.inf
    best_epoch = 0
    best_checkpoint = ""
    optimizer_step_count = 0
    for epoch in range(1, protocol.epochs + 1):
        model.train()
        total_loss = 0.0
        batch_count = 0
        for batch_index, raw_batch in enumerate(train_batches, start=1):
            batch = _move_batch(raw_batch, device)
            autocast = (
                torch.autocast(
                    device_type=plan.autocast_device_type,
                    dtype=plan.autocast_dtype,
                )
                if plan.autocast_device_type
                else nullcontext()
            )
            with autocast:
                loss = _model_loss(model(**batch))
                scaled_loss = loss / accumulation_steps
            if not bool(torch.isfinite(loss).item()):
                raise FloatingPointError(f"non-finite training loss at epoch {epoch}")
            scaled_loss.backward()
            total_loss += float(loss.detach().cpu().item())
            batch_count += 1
            should_step = (
                batch_index % accumulation_steps == 0
                or batch_index == batches_per_epoch
            )
            if should_step:
                torch.nn.utils.clip_grad_norm_(
                    [p for p in model.parameters() if p.requires_grad],
                    protocol.max_grad_norm,
                )
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
                optimizer_step_count += 1

        accuracy = validation_accuracy(model, validation_batches, device)
        epoch_record = {
            "epoch": epoch,
            "mean_train_loss": total_loss / batch_count,
            "validation_accuracy": accuracy,
        }
        history.append(epoch_record)
        # Strict improvement implements deterministic earliest-epoch ties.
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_epoch = epoch
            checkpoint = run_dir / "checkpoints" / f"epoch-{epoch:03d}"
            _save_trainable_checkpoint(
                model, checkpoint, epoch=epoch, validation_accuracy=accuracy
            )
            best_checkpoint = str(checkpoint)

    # The caller receives the selected model, not merely the final epoch.  This
    # keeps the official-test evaluation path mechanically tied to selection.
    load_trainable_checkpoint(model, Path(best_checkpoint))
    result = TrainingResult(
        best_epoch=best_epoch,
        best_validation_accuracy=best_accuracy,
        best_checkpoint=best_checkpoint,
        epochs=tuple(history),
        precision=plan.name,
        optimizer_steps=optimizer_step_count,
        trainable_boundary=boundary.to_dict(),
    )
    (run_dir / "training_result.json").write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
