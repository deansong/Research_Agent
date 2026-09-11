# Hotel model-association preregistration v1

This preregistration defines an English-language, globally unqualified construct
before checkpoint scoring. Models are time-indexed statistical instruments. All
quantities concern associations encoded by exact checkpoint revisions under a
fixed prompt design. They do not measure employees, vacancies, regional labour
markets, causality, population uncertainty, or true historical skill demand.

## Locked maintained components

- `configs/hotel_prompts.v1.json` fixes four semantic paraphrases whose context
  ends exactly at the target slot, exact target-boundary rules, a neutral
  `global_unqualified` stratum, five M49-labelled geographic sensitivity strata,
  and seven positive/negative control pairs.
- `configs/hotel_estimands.v1.json` fixes scoring mathematics, normalization,
  ranks, adjacent documented-cutoff contrasts, effect sizes, stability,
  missingness, resampling units, tests, and multiplicity families.
- `configs/hotel_sensitivities.v1.json` assigns independent namespaces and
  manifests to prompt, geography, scoring method, tokenizer, scale,
  architecture, role-taxonomy, and skill-taxonomy analyses. It labels every
  proposed contrast as isolated or confounded and prohibits primary pooling.
- `configs/hotel_skill_sources.v1.json` maps every researcher-frozen primary
  skill to one or more ESCO 1.2.0 concepts. This terminology provenance is
  independent of the O*NET role mapping and analytically sealed O*NET
  validation evidence.

The primary model panel, roles, skill surfaces and validation reserve remain in
the corresponding `configs/hotel_*v1.json` files described by
`docs/HOTEL_STUDY_INPUTS.md`.

## Prompt cells and boundaries

The primary design crosses 2 pinned base models, 7 roles, 28 skills, 4 prompt
units and 1 neutral stratum: 1,568 analytical cells. The geographic sensitivity
adds 7,840 analytical cells from the same factors and five first-level region
labels. Matched positive/negative controls add 112 neutral and 560 geographic
cells. The generated inventory therefore contains 10,080 unique exact cell
identities.

Every template literally ends with `{skill_slot}`. Rendering removes only that
marker, leaves no trailing whitespace, and appends one ASCII space plus the
frozen target phrase. No punctuation, EOS, newline, instruction, question,
answer choice, system message, chat template, or generated completion is added.
Token boundaries are qualified from the complete context-plus-target string for
each native pinned tokenizer.

The five geographic labels—Africa, the Americas, Asia, Europe, and Oceania—are
the first-level regions in the [United Nations M49
standard](https://unstats.un.org/unsd/methodology/m49/). They are controlled
lexical context labels, not sampled geographic units, and their results cannot
be characterized as regional labour-market evidence.

## Scores and inference

For every target token, the scorer must retain its raw target logit, the
full-vocabulary log-sum-exp normalization term, and their difference (the token
log probability). Total sequence log probability is the primary cell score.
Mean-token log probability is a secondary scoring-method sensitivity computed
from the same tokens; it is never treated as the same estimand or pooled with
the primary score.

Scores are normalized only within model × role × prompt × stratum × scoring
method over the complete frozen 28-skill set, using a population-standard-
deviation z score. Four prompts are equally averaged for role-skill estimates;
seven roles are equally averaged for the global-unqualified skill summary. The
weights are design weights, not population, employment, vacancy, or regional
weights. Raw log probabilities are never compared between models.

The one primary adjacent contrast is Llama 3.1 8B base (publisher-documented
December 2023 pretraining cutoff) minus Llama 3 8B base
(publisher-documented March 2023 cutoff). It is a checkpoint-association shift,
not a causal effect of time. Descending within-model midranks, rank shifts,
fixed-design Cohen's dz, prompt rank correlations, top-five overlap, and
leave-one-prompt-out ranges are registered secondary summaries.

The only resampled unit is the semantic template ID. Each replicate draws four
of the four fixed templates with replacement, and the same draw is applied
jointly across models, skills and roles; roles and geographic labels are never
resampled. The 10,000-replicate percentile ranges are called **fixed-design
prompt sensitivity intervals**. They are not confidence intervals and do not
represent population, model-training, employee, vacancy, or labour-market
uncertainty.

Primary total-score role-skill sign-flip tests form one 196-member
Benjamini–Hochberg family; 28 global skill tests form a separate Holm family.
Mean-token parallels are two separate secondary families. Results are never
filtered to adjusted significance. Missing cells are not imputed or replaced:
normalization requires all 28 skills, role estimates require all four paired
prompts, and global estimates require all seven roles.

## Generated review and source material

All generated material is under the required run directory:

`/export/ssd/gate/users/xingyi/goose/dummy_agent/codex_langgraph_agent/.agent/sessions/build-and-execute-a-reproducible-6559d3/artifacts`

`preregistration_v1/manifests/registered_cells.v1.jsonl` is the exact cell
inventory. `preregistration_v1/manifests/sensitivities/` contains separate
namespace manifests. `preregistration_v1/evidence/` contains the archived M49
page, O*NET role-definition extraction, tokenizer evidence, and source
manifest. `study_inputs_v1/evidence/extracted/` contains a deterministic catalog
of the 15,163 English skill concepts extracted from the official ESCO archive.

Rebuild and validate deterministically:

```bash
python .agent/sessions/build-and-execute-a-reproducible-6559d3/artifacts/study_inputs_v1/extract_esco_english.py
python .agent/sessions/build-and-execute-a-reproducible-6559d3/artifacts/study_inputs_v1/match_esco_candidates.py
python .agent/sessions/build-and-execute-a-reproducible-6559d3/artifacts/preregistration_v1/build_preregistration.py
python .agent/sessions/build-and-execute-a-reproducible-6559d3/artifacts/preregistration_v1/generate_token_length_classes.py
python .agent/sessions/build-and-execute-a-reproducible-6559d3/artifacts/preregistration_v1/build_preregistration.py
```

The first pinned Llama 3 tokenizer qualified all 5,040 of its registered cells.
Hugging Face denied access to the pinned Llama 3.1 8B repository on 2026-09-08,
so all 5,040 corresponding tokenizer-qualification cells are explicitly marked
missing. No mirror or tokenizer substitution was used. The review packet is
content-addressed but deliberately marked `draft_unapproved`; it authorizes no
scoring. Approval of a later bound packet is a separate plan step, and any
design-changing edit requires a new digest and approval.

## Occupational-source separation

O*NET 31.0 `Occupation Data` supplies the role codes, titles, and definitions.
The [O*NET database page](https://www.onetcenter.org/database.html) documents
the downloadable fixed release and CC BY 4.0 terms. Only `Occupation Data` was
opened for the mapping review. The reserved task, skill, knowledge, activity,
style, and software tables remain sealed until model-only analysis is frozen.

ESCO 1.2.0 supplies independent skill terminology. The [European Commission
download page](https://esco.ec.europa.eu/en/use-esco/download) identifies the
fixed release, while the [ESCO API documentation](https://esco.ec.europa.eu/en/use-esco/use-esco-services-api/esco-web-service-api)
explains that concepts have stable URIs and supports explicit version selection.
Aliases remain display/mapping aids and are not extra primary observations.

## Repaired complete sensitivity freeze

Review issue 3 replaces every zero-cell placeholder. Each required namespace
now has a non-empty manifest and a separate exact expected-cell JSONL inventory.
Registered counts are: prompt 1,568; geography 8,400 including controls;
scoring method 1,568; tokenizer 1,680; scale 1,680; architecture 1,680; role
taxonomy 2,688; broad-construct skill taxonomy 1,232; operational-granularity
skill taxonomy 1,792; instruction-tuned 1,680; and unknown-cutoff 840.

Instruction checkpoints use the same plain completion rendering and no chat
template. This does not isolate chat rendering and remains a separately
confounded instruction-tuning sensitivity. The scale inventory is Llama 3.1 8B
base versus Llama 3.1 70B base at the same documented cutoff. Architecture and
tokenizer namespaces separately register the same Llama 2 7B versus Llama 3 8B
cells and retain their explicit confounding labels. Unavailable or infeasible
resources never remove expected cells: they create failure-ledger and
missing-cell records, with no substitution.

## Native-chat rendering sensitivity

The separate `sensitivity/chat_template/v1` namespace crosses the two pinned
instruction-tuned checkpoints, seven primary roles, 28 primary skills, two
role-matched controls, and four semantic prompts for 1,680 exact cells. For
each cell, the registered plain context is the only user message, no system
message or tools are supplied, the pinned native template is rendered with
`add_generation_prompt=true`, and one ASCII space plus the frozen target phrase
is appended after the assistant header without EOS. The archived template
SHA-256 and exact model/tokenizer revisions are bound in the sensitivity model
registry and manifest.

The pinned Llama 3.1 template itself emits an empty system section, a
`Cutting Knowledge Date: December 2023` line, and its fixed default
`Today Date: 26 Jul 2024` line. These are template text, not evidence of an
additional corpus cutoff. Native templates differ across model revisions, so
cross-model chat comparisons remain explicitly confounded. Within each
checkpoint, chat versus plain completion is the registered rendering
sensitivity.
