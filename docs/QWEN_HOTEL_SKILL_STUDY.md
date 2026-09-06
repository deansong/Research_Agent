# Qwen hotel-skill release-generation study

This branch compares elicited model representations from two official,
revision-pinned Qwen releases. It does **not** measure hotel staff skills,
vacancies, labour demand, skill emergence, or historical change. Qwen does not
publish a defensible training-data cutoff for either selected checkpoint, so
the only time axis is the official model release generation.

## Selected checkpoints

| model | immutable revision | release ordering | loader |
| --- | --- | --- | --- |
| `Qwen/Qwen1.5-0.5B-Chat` | `4d14e384a4b037942bb3f3016665157c8bcb70ea` | 2024-02-04 | `AutoModelForCausalLM` |
| `Qwen/Qwen3.5-0.8B` | `2fc06364715b967f1860aea9cf38778875588b17` | 2026-03-02 | `AutoModelForImageTextToText` |

The model and tokenizer use the same pinned snapshot in each row. Both use
their own checkpoint chat template while receiving semantically identical
text. Qwen3.5 thinking is disabled for direct constrained answers. BF16 is
requested explicitly and is not silently weakened. Repository SHAs and weight
sizes were verified through the Hugging Face Hub API on 2026-09-06; release
evidence is recorded in `configs/qwen_models.json`.

## Completed preflight and pending review gate

```bash
python -m hotel_skill_study.cli validate --models configs/qwen_models.json
python -m hotel_skill_study.cli discover --backend fixture \
  --models configs/qwen_models.json --fixtures fixtures/qwen_discovery_outputs.jsonl \
  --run-dir artifacts/qwen_fixture_demo
python -m hotel_skill_study.cli taxonomy --run-dir artifacts/qwen_fixture_demo \
  --output artifacts/qwen_taxonomy/proposal.json
```

The fixture is hand-authored software-test data, not checkpoint output. Its
proposal digest is `3095201c9ad6c3243f2a60a63dc4959e4d3252a54a4f8b7af3f6305a55ca03cc`.
No genuine human approval has been supplied, and
`artifacts/qwen_taxonomy/approval.json` does not exist. The proposal remains
unapproved. An earlier unsupported approval record and its derived synthetic run
are quarantined in `artifacts/qwen_invalidated/` and must not be used.

Synthetic software verification uses only
`artifacts/qwen_taxonomy/fixture_test_approval.json`. This scope is explicitly
rejected by the empirical Transformers backend. The regenerated fixture run
completed 84/84 cells with zero missing cells and passed artifact validation.
Its outputs remain synthetic and cannot support model findings. Any proposal
change requires a new digest and review.

Only after genuine human approval, reassess GPU and CPU execution options and
use fresh, empirical-only directories. Keep the Hugging Face cache on the
workspace disk because `/tmp` is full on the audited host. Download and execute
checkpoints sequentially; the scorer already unloads each model and retains
failed cells.

Each target answer token retains its raw target logit and the full-vocabulary
`logsumexp`, which reproduces its log probability without dumping the full
vocabulary. Cross-model summaries use constrained Yes/No probabilities and
within-model ranks; raw logits are never treated as calibrated across the two
different tokenizers.
