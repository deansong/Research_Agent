# Frozen hotel-study inputs (v1)

The 2026-09-08 input freeze treats checkpoints only as time-indexed statistical
instruments. It defines associations encoded under controlled prompts; it does
not measure employees, vacancies, regional labour markets, causality,
population uncertainty, or true historical skill demand.

## Maintained records

- `configs/hotel_study_construct.v1.json` defines the construct and prohibited
  interpretations.
- `configs/hotel_model_panels.v1.json` pins models and tokenizers and separates
  the primary, instruction, scale, architecture/tokenizer, and unknown-cutoff
  panels.
- `configs/hotel_roles.v1.json` maps seven experimental role families to O*NET
  31.0 occupation codes with explicit boundaries.
- `configs/hotel_skills.v1.json` freezes the independently defined primary
  vocabulary, aliases, rules, surface-length classes, and two alternatives.
- `configs/hotel_skill_sources.v1.json` links every frozen primary skill to
  reviewed ESCO 1.2.0 concept URIs without using model scores or reserved O*NET
  validation ratings.
- `configs/hotel_validation_reserve.v1.json` seals O*NET occupational content
  for broad post-analysis validation only.

The prompt and inference freeze is documented separately in
`docs/HOTEL_STUDY_PREREGISTRATION.md`.

## Primary model panel

The primary panel contains the raw pretrained base checkpoints
`meta-llama/Meta-Llama-3-8B` and `meta-llama/Llama-3.1-8B`. Meta documents their
pretraining cutoffs as March 2023 and December 2023, respectively. Their release
dates—April 18 and July 23, 2024—are separate metadata and are not used to infer
cutoffs. Both checkpoint and tokenizer repositories are pinned to full immutable
Hugging Face commit hashes.

This is the strongest available controlled subset recorded here: same publisher,
nominal 8B scale, decoder-only Llama 3 family, tokenizer family, raw-logit loader,
and BF16 unquantized weights. It is not a controlled intervention on time. The
later model uses a new data mix and long-context RoPE scaling, so findings are
model-version associations and cannot be attributed causally to the cutoff.

Both repositories are manually gated. No real run may silently substitute a
mirror, quantization, instruction model, moving revision, or fixture. Preflight
must verify license acceptance, authentication, exact revisions, tokenizer
rendering, full-vocabulary logits, and memory. A failed or unavailable cell must
enter the failure ledger and missing-cell inventory.

The legacy `configs/models.json` pair is deprecated and deselected. Qwen release
ordering remains an unknown-cutoff sensitivity only. Llama 2, 70B scale, and
instruction checkpoints are separately named sensitivities and may never be
pooled into the primary cutoff series.

## Roles, skills, and validation separation

O*NET 31.0 provides source codes and definitions for front office, housekeeping,
food and beverage, maintenance/engineering, sales and revenue, security, and
management. The sales/revenue role is an explicit two-code composite because
O*NET does not define a hotel revenue-manager occupation.

The primary 28-skill vocabulary was frozen before model scoring. ESCO 1.2.0 is
the independent terminology/domain source. Aliases do not create duplicate
primary observations. Whitespace-based surface classes are recorded now; actual
target token IDs and token-count classes must be derived separately with each
pinned tokenizer during qualification and then bound into the design packet.

O*NET 31.0 task, skill, knowledge, activity, style, and software tables are
reserved. They may be opened for qualitative broad-pattern validation only
after the model-only analysis is frozen. They cannot select skills or revise
aliases, prompts, panels, or estimands, and they are contemporary occupational
evidence—not historical evidence for either model cutoff.

## Evidence archive

Raw publisher cards, repository metadata and file manifests, licenses, the ESCO
1.2.0 RDF archive, the O*NET 31.0 text database, source excerpts, and SHA-256
records are under the run-specific `study_inputs_v1` artifact directory required
by the execution environment. These are source archives, not empirical outputs.
No deleted fixture, approval, or historical output from `4914bf3` was restored.

Primary publisher evidence:

- [Meta Llama 3 model card](https://github.com/meta-llama/llama3/blob/a0940f9cf7065d45bb6675660f80d305c041a754/MODEL_CARD.md)
- [Meta Llama 3.1 model card](https://github.com/meta-llama/llama-models/blob/0e0b8c519242d5833d8c11bffc1232b77ad7f301/models/llama3_1/MODEL_CARD.md)
- [ESCO download documentation](https://esco.ec.europa.eu/en/use-esco/download)
- [O*NET database documentation](https://www.onetcenter.org/database.html)

## Input-digest and worktree disclosure

The original supported input-freeze digest is
`dc991cadfb0cce5e9f1228fca3b53bd08e9f6efcd827dff0c9228ec453902ad2`.
No repository or artifact record supports the previously claimed digest prefix
`814e569c`; it is rejected. The repaired model-panel and alternative-taxonomy
identities are frozen by `input_freeze_manifest.v2.json`, which preserves the
original digest as its predecessor and computes a new digest over the changed
maintained inputs.

The repository worktree is disclosed as dirty at review issue 3: two tracked
files are modified and twelve study files are untracked. Approval binds the
exact bytes named in the packet rather than claiming a clean tree. Any later
byte change invalidates that approval.
