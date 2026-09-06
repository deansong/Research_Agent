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

## Completed preflight and human review

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
The human explicitly answered `/approve` to the review request for digest
`3095201c9ad6c3243f2a60a63dc4959e4d3252a54a4f8b7af3f6305a55ca03cc`.
`artifacts/qwen_taxonomy/approval.json` preserves the verbatim command and blank
notes. An earlier unsupported approval record and its derived synthetic run are
still quarantined in `artifacts/qwen_invalidated/` and must not be used.

Synthetic software verification uses only
`artifacts/qwen_taxonomy/fixture_test_approval.json`. This scope is explicitly
rejected by the empirical Transformers backend. The regenerated fixture run
completed 84/84 cells with zero missing cells and passed artifact validation.
Its outputs remain synthetic and cannot support model findings. Any proposal
change requires a new digest and review.

The empirical run uses `artifacts/qwen_score_run` and the ignored workspace-local
cache `hotel_skill_study/.model_cache/`. CUDA is preferred when available;
otherwise the same pinned BF16 checkpoints are attempted on CPU. Checkpoints run
sequentially, are unloaded between models, and every load/scoring failure is
retained rather than replaced.

The completed run produced 84/84 successful scoring cells. Although NVML and
`nvidia-smi` probes failed, each empirical record reports `cuda:0`; runtime
records are authoritative. The first invocation encountered environment cache
overrides, so both exact snapshots were subsequently populated explicitly into
the 2.8 GB workspace-local cache. An offline rerun reused all 84 cached cells
without checkpoint loading. Qwen3.5 reported that its optional fast path was
unavailable and used the Torch implementation; this was not hidden or replaced.

Each target answer token retains its raw target logit and the full-vocabulary
`logsumexp`, which reproduces its log probability without dumping the full
vocabulary. Cross-model summaries use constrained Yes/No probabilities and
within-model ranks; raw logits are never treated as calibrated across the two
different tokenizers.

## Empirical findings: binary and direct phrase completion

The validated binary run remains in `artifacts/qwen_score_run`. It measures the
probability of the answer tokens ` Yes` versus ` No` after a question containing
the skill phrase. It does not contain skill-phrase target logits and is not used
as a substitute for direct completion.

The separate `artifacts/qwen_phrase_score_run` teacher-forces every complete
approved phrase after three prompts that do not reveal the target skill. All 84
cells succeeded. The 2- or 3-token phrases produced 180 token observations; each
retains target logit, full-vocabulary logsumexp, and reconstructed log
probability. Sequence-total and length-normalized results are both retained and
normalized only within each model/prompt over the 14 approved phrases.

Under the primary length-normalized summary, both releases rank **Attention to
detail**, **Multitasking**, and **Customer service** first, second, and third.
The largest adjacent-release rank movement is **Guest communication**, from 11
in Qwen1.5 to 4 in Qwen3.5. **Payment processing** moves from 5 to 8 and
**Problem solving** from 7 to 9. **AI literacy** is near the bottom in both (13
to 14). Prompt ranges are substantial for several leading phrases, especially
Attention to detail, Multitasking, and Customer service, so these ranks are
descriptive and prompt-dependent.

These findings characterize two model releases under a constrained English
prompt design. They do not establish changing hotel occupations, worker skills,
vacancy demand, or skill emergence. Raw logits are never compared across the
different vocabularies. See `artifacts/qwen_phrase_score_run/REPORT.md`, the
per-model tables, adjacent-release rank table, prompt-sensitivity table, and
explicit failure ledger for the complete results.

## Pending Qwen taxonomy-sensitivity review

The review-ready alternative at
`artifacts/qwen_taxonomy_alternatives/ambiguous_splits_v1/` is derived from base
digest `3095201c9ad6c3243f2a60a63dc4959e4d3252a54a4f8b7af3f6305a55ca03cc`.
It splits Conflict resolution, Cash handling, PMS proficiency/property-management
systems, Accuracy, and Prioritization from their ambiguous base merges while
leaving every other mapping unchanged. It contains 19 categories, 144 mappings,
and complete coverage. Its exact digest is
`2de4ced503de820a2474a97e17eb7794714906d64f88bdd2a3691d7b4ebdea9a`.

The alternative is proposed and unapproved. The base approval does not apply,
and neither binary nor direct-phrase scoring has been run for it. `REVIEW.md`,
`categories.csv`, `raw_phrase_mappings.csv`, `fixture_prevalence.svg`, and
`review_manifest.json` provide the rationale, aliases, per-model fixture
coverage, provenance, hashes, and approval consequences for separate review.
