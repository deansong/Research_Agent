# Hotel skill discovery and comparison study

> **Superseded design notice (2026-09-08):** the GPT-2/Llama-3-Instruct
> default described below is a rejected legacy design and must not be executed
> as the primary temporal panel. The maintained study-input freeze is documented
> in `docs/HOTEL_STUDY_INPUTS.md` and `configs/hotel_model_panels.v1.json`.
> Historical fixture approvals and outputs mentioned below were removed by
> `4914bf3` and are not admissible evidence.

## Scope and interpretation

This workflow compares **model representations** elicited from locally runnable,
revision-pinned checkpoints with documented and meaningfully different training
data periods. It does not measure historical vacancies, worker capabilities, or
labour-market demand. A change can arise from pretraining data, instruction
tuning, architecture, tokenizer, alignment, prompt adherence, or sampling.

The default role is `front-desk employee`. Change `role.id`, `display_name`, and
`industry` in `configs/hotel_skill_study.json` for another hotel role. The same
rendered role is applied to every compatible model.

## Checkpoint policy and evidence

`configs/models.json` separates `release_date` from `cutoff`. Cutoff status is one
of `documented_cutoff`, `documented_period`, `inferred`, or `unknown`; selected
models must use one of the documented statuses and an immutable model and
tokenizer revision. An excluded Mistral entry demonstrates that release date is
not accepted as a substitute for cutoff evidence.

The initial selected pair is intentionally far apart:

| checkpoint | period used by study | primary evidence | warning |
| --- | --- | --- | --- |
| `openai-community/gpt2` | WebText links selected from Reddit posts before Dec 2017 | [GPT-2 report](https://cdn.openai.com/better-language-models/language_models_are_unsupervised_multitask_learners.pdf) | Corpus collection rule, not proof every linked page predates it; base model |
| `meta-llama/Meta-Llama-3-8B-Instruct` | 8B pretraining cutoff March 2023 | [Meta Llama 3 model card](https://github.com/meta-llama/llama3/blob/main/MODEL_CARD.md) | Fine-tuning freshness is incompletely dated; instruction-tuned and gated |

The Hugging Face revisions are pinned in the manifest. These pins identify the
downloaded packaging and weights; the primary cutoff evidence remains the model
authors' report/card. Before a real run, re-check all linked evidence and record
any correction as a manifest commit, never as an undocumented local override.

## Install and acquire

Fixture discovery needs only the repository's normal Python environment. Local
inference additionally needs:

```bash
pip install torch transformers accelerate
huggingface-cli login  # accept the Meta Llama 3 licence first
huggingface-cli download openai-community/gpt2 --revision 607a30d783dfa663caf39e06633721c8d4cfcd7e
huggingface-cli download meta-llama/Meta-Llama-3-8B-Instruct --revision 8afb486c1db24fe5011ec46dfbe5b5dccdb575c2
```

GPT-2 is small; Llama 3 8B requires roughly 16 GB for BF16 weights plus runtime
overhead, or suitable automatic device placement. Exact memory and time depend
on dtype, hardware, Transformers, and cache state. This repository did not
download either checkpoint or execute full inference during the proposal run.

## Stages and commands

Validate first:

```bash
python -m hotel_skill_study.cli validate
```

Run the labelled demo and propose the taxonomy:

```bash
python -m hotel_skill_study.cli discover --backend fixture --run-dir artifacts/demo_run
python -m hotel_skill_study.cli taxonomy --run-dir artifacts/demo_run \
  --output artifacts/taxonomy/proposal.json
```

For real generation, replace `fixture` with `transformers` and use a new run
directory. Generation covers the Cartesian product of selected models, three
prompt variants, and three seeds. Every attempt becomes one atomic JSON record,
including failures. The content-addressed filename incorporates backend,
fixture-source digest (when applicable), checkpoint and tokenizer identities,
architecture/adapter, complete role and prompt variant, rendered prompt, seed,
and generation parameters. Reruns skip only records whose embedded identity is
an exact match. Reusing a directory with a different backend, config, role,
prompt, or tokenizer is rejected before inference; use a new directory. A run
manifest preserves the complete run identity.

Fixture strings are hand-authored software-test data. Every record and the
taxonomy say `fixture: true`; they are not represented as outputs of GPT-2 or
Llama 3 and support no empirical model-period conclusion.

## Taxonomy method and review gate

Extraction takes concise non-empty list lines. Normalization case-folds, removes
punctuation, and collapses whitespace. Clustering is deliberately conservative:
only exact normalized aliases in `configs/synonyms.json` merge automatically.
Everything else remains its own category with `unclustered` and
`human_review_required`. Every occurrence retains raw spelling, line number,
record, model, prompt, seed, confidence, and flags. Categories retain counts,
raw variants, examples, and model counts. `model_specific_in_fixture` is a
review cue, not an emergence claim.

The human reviewer approved taxonomy `v1` at digest
`d5270d9bd619f1316fcfb599145c51919f1d0e6110f1a6eb8cddb8c20eeecdab`.
The immutable approval record is `artifacts/taxonomy/approval.json`; the blank
human-notes field and all researcher review questions are preserved in
`artifacts/taxonomy/DECISIONS.md`. The proposed merges and unclustered `Record
keeping` category were not silently changed. For a future proposal, review
`proposal.json`, record reasoning in `DECISIONS.md`, and approve its exact digest:

```bash
python -m hotel_skill_study.cli approve \
  --taxonomy artifacts/taxonomy/proposal.json \
  --approval artifacts/taxonomy/approval.json \
  --reviewer "REVIEWER NAME" --note "Reviewed categories and flagged merges"
```

Any taxonomy edit changes its digest and invalidates the approval. Regenerate
the proposal after editing synonyms, preserve the old proposal and decision in
version control, then review again. Do not reuse the v1 approval for a changed
taxonomy.

## Post-approval scoring and outputs

After approval only:

```bash
python -m hotel_skill_study.cli score \
  --taxonomy artifacts/taxonomy/proposal.json \
  --approval artifacts/taxonomy/approval.json \
  --output-dir artifacts/score_run
```

The command defaults to `--backend transformers`. A complete, network-free
software verification uses deterministic synthetic values:

```bash
python -m hotel_skill_study.cli score --backend fixture \
  --taxonomy artifacts/taxonomy/proposal.json \
  --approval artifacts/taxonomy/approval.json \
  --output-dir artifacts/fixture_score_run
python -m hotel_skill_study.cli validate-artifacts \
  --taxonomy artifacts/taxonomy/proposal.json \
  --approval artifacts/taxonomy/approval.json \
  --output-dir artifacts/fixture_score_run
```

Every fixture record, manifest, table, and plot is labelled synthetic. These
values exercise unequal answer-token lengths, ranking, uncertainty, and
reporting code only. They were not generated by either named checkpoint and
must never be cited as empirical results.

For each model, approved skill, and three scoring phrasings, the scorer computes
teacher-forced sequence log probability for the exact answer strings ` Yes` and
` No`. It retains every answer token and component log probability. Their
two-choice softmax is a constrained, within-model calibration. It is explicitly
labelled `model_relative_constrained_choice`; raw next-token probabilities are
never compared as if tokenizers shared a scale. Summaries include prompt range,
descriptive prompt-resampling intervals, bootstrapped rank ranges, point-rank
stability, and within-model ranks. These intervals resample only three fixed
prompt variants (`n=3`), are not population confidence intervals, and do not
quantify model-training or labour-market sampling uncertainty. Discovery prevalence separately
bootstraps repeated prompt/seed records. The score cache key includes the exact
approved taxonomy, backend, checkpoint and tokenizer identifiers/revisions,
architecture/adapter, role/industry, skill label and ID, full prompt variant and
rendered prompt, scoring method, and answer choices. The output-directory run
identity is also checked before checkpoint loading. Reruns skip only exact cells.
As a scoring sensitivity check, the pipeline also recomputes ranks using mean
token log probability rather than total sequence log probability and writes the
rank changes to `scoring_method_sensitivity.csv`.
The rank/interval SVG and CSV are reproducible from record JSON. Failed model
loads and scoring cells are retained, excluded from numeric summaries, and
counted in the score-run manifest.

The implemented `reviewed_splits_v1` alternative separates conflict resolution,
cash handling, PMS proficiency, accuracy, and prioritization while leaving every
other approved-v1 mapping unchanged. It is always a new proposal and digest:

```bash
python -m hotel_skill_study.cli taxonomy-alternative \
  --taxonomy artifacts/taxonomy/proposal.json \
  --output artifacts/taxonomy_alternatives/reviewed_splits_v1/proposal.json
# Human reviews the new artifact, then runs `approve --scope human`.
python -m hotel_skill_study.cli score --backend transformers \
  --taxonomy artifacts/taxonomy_alternatives/reviewed_splits_v1/proposal.json \
  --approval artifacts/taxonomy_alternatives/reviewed_splits_v1/approval.json \
  --output-dir artifacts/score_run_reviewed_splits_v1
python -m hotel_skill_study.cli compare-taxonomies \
  --base-taxonomy artifacts/taxonomy/proposal.json \
  --base-approval artifacts/taxonomy/approval.json --base-run-dir artifacts/score_run \
  --alternative-taxonomy artifacts/taxonomy_alternatives/reviewed_splits_v1/proposal.json \
  --alternative-approval artifacts/taxonomy_alternatives/reviewed_splits_v1/approval.json \
  --alternative-run-dir artifacts/score_run_reviewed_splits_v1 \
  --output-dir artifacts/taxonomy_comparison_reviewed_splits_v1
```

For automated no-model tests only, `approve --scope fixture_test` authorizes a
fixture-derived alternative for `--backend fixture`; it is explicitly rejected
for real checkpoint scoring. The comparison reports category/mapping coverage,
ranks, rank direction, descriptive intervals, and prompt ranges, including
categories present in only one version. Do not pool missing cells. An effect is robust
only if its qualitative conclusion survives prompt and reasonable taxonomy
choices. This workflow supplies traceable inputs for such analysis; it does not
pre-author a conclusion.

## Traceability and limitations

The path is taxonomy category → mapping → `record_id` → raw discovery record →
run manifest/config hashes. Scoring adds taxonomy and approval hashes, checkpoint
and tokenizer revisions, prompt, exact choices, component log probabilities,
runtime versions, summary row, and plot. Summary JSON/CSV retain component
record IDs; explicit missing-cell JSON is emitted even when empty. The artifact
validator recomputes expected cell identities and record keys; checks prompts,
configs, component sums/means, both calibrated scores, summaries and record
references, missing-cell consistency, manifest config hashes, intrinsic fixture
labels, and derived table/plot hashes.
Git commit and hardware/driver metadata should be captured alongside any
publication run.

Key limitations are the two-model sample, GPT-2 base versus Llama 3 instruction
tuning, incomplete visibility into training/fine-tuning corpora, stochastic and
hardware variation, English-only prompts, researcher-authored synonym aliases,
and construct validity of prompted skill lists. “AI literacy” must be treated as
potentially emergent language in the elicitation—not proof of labour-market
emergence.

## Tests

```bash
python tests/test_hotel_skill_study.py
```

The tests use no network or checkpoint downloads and cover validation, complete
fixture discovery, caching/resumption, mapping coverage, emergence flags,
approval blocking and digest matching, deterministic bootstrap intervals,
scoring calibration, 84-cell fixture scoring, resumption, traceability, and
artifact validation.

## Execution record

Executed in this repository on 2026-09-06:

- configuration and Python syntax validation;
- discovery/taxonomy focused tests using 18 labelled fixture records;
- post-approval synthetic scoring for 2 configured models × 14 approved skills
  × 3 scoring prompts = 84 successful fixture cells;
- fixture-only sensitivity scoring for 19 alternative categories × 2 model
  labels × 3 prompts = 114 successful cells, followed by 38 base/alternative
  score comparisons and 19 rank-direction comparisons;
- output validation with zero missing successful cells.

Not executed: checkpoint download, GPT-2 generation/scoring, Llama 3
generation/scoring, GPU inference, or any empirical comparison. To perform the
real run, acquire the pinned revisions above, create a new real discovery run
and (if its taxonomy differs) approve that taxonomy, then invoke `score` without
`--backend fixture`. Preserve failures; do not replace them with fixture values.
