# Repository provenance audit: hotel-skill study

Audit date: 2026-09-07 (Europe/London)  
Audited revision: `32b4bd4414c0272f26f2b5739a7faf4d7ee286fa` (`master`, also `origin/master`)  
Removal commit: `4914bf32384a4d5c2e6aac46cf61c4d4ee04dba6`  
Removal parent: `eeca741877303e0e75305a173e223d3b4663135f`

## Scope and conclusion

This is a source-provenance audit, not an empirical result. It was made from
the current index and worktree, retained project files, and Git objects. No
historical output was copied into the worktree or treated as evidence.

The old implementation contains useful engineering ideas, but no old study
artifact is admissible evidence for the proposed study. The historical model
panels, occupational scope, vocabulary construction, prompts, approval, and
inference plan do not meet the new design. Reuse must therefore mean a reviewed
reimplementation of a traceable idea, with new schemas and tests; it must not
mean restoring the deleted package, fixtures, approvals, records, tables,
plots, or reports wholesale.

## Current repository state

At the audit revision:

- `git status --short --branch` reports `master...origin/master` with no
  tracked modifications, staged changes, or ordinary untracked project files.
  Session infrastructure is outside the study audit and was excluded as
  required.
- The index contains 97 files. The hotel-specific tracked files are
  `configs/hotel_skill_study.json`, `configs/models.json`,
  `configs/qwen_models.json`, `configs/qwen_phrase_scoring.json`,
  `configs/synonyms.json`, `docs/HOTEL_SKILL_STUDY.md`, and
  `docs/QWEN_HOTEL_SKILL_STUDY.md`.
- There is no tracked `hotel_skill_study/` implementation, `fixtures/`
  directory, empirical artifact tree, or
  `tests/test_hotel_skill_study.py`. The normal `requirements.txt` also has no
  PyTorch, Transformers, Accelerate, Hugging Face Hub, statistical, or plotting
  dependencies for this study.
- No repository `AGENTS.md` applies. The repository's session-infrastructure
  rules remain controlling for generated material.
- `.gitignore` still names `hotel_skill_study/.model_cache/`, a stale path from
  the removed implementation. Future caches belong in the assigned run
  artifacts directory, not in the source tree.

The two retained study documents are historical descriptions, not accurate
current execution instructions. Both refer to absent commands and artifacts.
`docs/QWEN_HOTEL_SKILL_STUDY.md` additionally states that empirical runs were
completed even though their records and report were deliberately removed.
Those statements are useful provenance warnings only and must not be cited as
results.

## History and provenance

The relevant sequence is unusually compressed into 2026-09-06 and is not a
clean, dedicated study history:

| Commit | Recorded role in the history | Provenance implication |
| --- | --- | --- |
| `a1748f7` | Nominally fixes agent sessions/progress, but also first adds the original hotel package, configs, docs, fixtures, synthetic taxonomy, approvals, and fixture outputs. | Study material was swept into an unrelated agent commit. Commit subject and body do not review the scientific design or sources. |
| `90697d9` | Nominally fixes backend options; also first adds the Qwen model config and Qwen study document and expands package/tests/artifacts. | Qwen design and generated material again lack a dedicated provenance commit. |
| `466ff1c` | Introduces the session-artifact location rule; also adds direct phrase scoring and the retained Qwen phrase/binary outputs. | The commit body itself says a real run had scattered products into the project. It is the immediate source of the phrase report and most deleted Qwen records. |
| `cd8a990` | Moves ten run-output directories and a reported 2.8 GB model cache out of the project root. Four artifact directories are deliberately retained because the old tests read them as fixtures. | Generated outputs had been committed as source; test dependencies, not evidentiary status, determined what remained. |
| `40229e7` | Untracks the moved session outputs after the move left them indexed. | Confirms artifact placement and tracking were corrected after generation. Material moved into session infrastructure was not inspected in this audit. |
| `4914bf3` | “Remove the hotel-skill study.” Deletes 216 files and 29,556 lines, while deliberately retaining `configs/*.json` and the two study documents. | The commit explicitly calls the package, fixtures, and sample data an earlier experiment unrelated to the agent. It is the authoritative reason they are absent now. |

The artifacts' internal claims do not repair this commit-level provenance. The
retained docs describe fixture-only GPT-2/Llama verification and a later Qwen
run, but the old design lacks a frozen external occupational source, independent
skill construction, complete publisher evidence packets, full environment
freeze, and whole-design approval. Historical approvals bind only generated
taxonomy proposal digests; they do not approve the model panel, roles, prompts,
estimands, exclusions, or multiplicity plan required now.

## Files removed by `4914bf3`

The deletion is exactly reproducible with:

```bash
git diff-tree --no-commit-id --name-status -r 4914bf3
```

All 216 entries are deletions. The following inventory lists every semantic
path and lossless path pattern; the two record patterns each stand for exactly
84 content-addressed SHA-256-named JSON files. Expanding the command above is
the canonical individual filename list (the hashes have no additional semantic
meaning).

| Removed path or exact set | Count | Classification |
| --- | ---: | --- |
| `hotel_skill_study/.agent/.gitignore` | 1 | Nested stale infrastructure marker; reject. |
| `hotel_skill_study/__init__.py` | 1 | Package marker; superseded. |
| `hotel_skill_study/cli.py` | 1 | Historical CLI. |
| `hotel_skill_study/pipeline.py` | 1 | Historical monolithic implementation (1,696 lines). |
| `tests/test_hotel_skill_study.py` | 1 | Historical network-free test module (393 lines). |
| `fixtures/discovery_outputs.jsonl` | 1 | Hand-authored synthetic discovery fixture. |
| `fixtures/qwen_discovery_outputs.jsonl` | 1 | Hand-authored synthetic Qwen discovery fixture. |
| `artifacts/qwen_phrase_score_run/records/<sha256>.json` | 84 | Claimed checkpoint phrase-score component records. |
| `artifacts/qwen_score_run/records/<sha256>.json` | 84 | Claimed checkpoint binary-score component records. |
| `artifacts/qwen_phrase_score_run/REPORT.md` | 1 | Historical empirical report. |
| `artifacts/qwen_phrase_score_run/failure_ledger.json` | 1 | Historical failure ledger. |
| `artifacts/qwen_phrase_score_run/phrase_run_manifest.json` | 1 | Historical phrase-run manifest. |
| `artifacts/qwen_phrase_score_run/tables/adjacent_release_phrase_ranks.csv` | 1 | Derived table. |
| `artifacts/qwen_phrase_score_run/tables/per_model/qwen1_5_0_5b_chat_2024.csv` | 1 | Derived table. |
| `artifacts/qwen_phrase_score_run/tables/per_model/qwen3_5_0_8b_2026.csv` | 1 | Derived table. |
| `artifacts/qwen_phrase_score_run/tables/phrase_measurements.csv` | 1 | Derived table. |
| `artifacts/qwen_phrase_score_run/tables/phrase_measurements.json` | 1 | Derived table. |
| `artifacts/qwen_phrase_score_run/tables/phrase_skill_summary.csv` | 1 | Derived table. |
| `artifacts/qwen_phrase_score_run/tables/phrase_skill_summary.json` | 1 | Derived table. |
| `artifacts/qwen_phrase_score_run/tables/prompt_sensitivity.csv` | 1 | Derived table. |
| `artifacts/qwen_score_run/score_run_manifest.json` | 1 | Historical binary-run manifest. |
| `artifacts/qwen_score_run/taxonomy_sensitivity.json` | 1 | Historical sensitivity plan/status. |
| `artifacts/qwen_score_run/plots/fixture_discovery_prevalence.svg` | 1 | Synthetic plot. |
| `artifacts/qwen_score_run/plots/prompt_sensitivity.svg` | 1 | Derived plot. |
| `artifacts/qwen_score_run/plots/rank_trends.svg` | 1 | Derived plot. |
| `artifacts/qwen_score_run/plots/score_uncertainty_and_ranks.svg` | 1 | Derived plot. |
| `artifacts/qwen_score_run/tables/fixture_discovery_prevalence.csv` | 1 | Synthetic table. |
| `artifacts/qwen_score_run/tables/missing_cells.csv` | 1 | Historical missingness table. |
| `artifacts/qwen_score_run/tables/missing_cells.json` | 1 | Historical missingness record. |
| `artifacts/qwen_score_run/tables/prompt_sensitivity.csv` | 1 | Derived table. |
| `artifacts/qwen_score_run/tables/rank_changes_across_model_periods.csv` | 1 | Derived table whose “periods” are release generations. |
| `artifacts/qwen_score_run/tables/scoring_method_sensitivity.csv` | 1 | Derived table. |
| `artifacts/qwen_score_run/tables/skill_scores.csv` | 1 | Derived table. |
| `artifacts/qwen_score_run/tables/skill_scores.json` | 1 | Derived table. |
| `artifacts/qwen_score_run/tables/within_model_rank_trends.csv` | 1 | Derived table. |
| `artifacts/qwen_taxonomy/REVIEW.md` | 1 | Historical review packet. |
| `artifacts/qwen_taxonomy/approval.json` | 1 | Human approval for one fixture-derived taxonomy digest. |
| `artifacts/qwen_taxonomy/fixture_test_approval.json` | 1 | Automated fixture-test authorization. |
| `artifacts/qwen_taxonomy/categories.csv` | 1 | Fixture-derived taxonomy view. |
| `artifacts/qwen_taxonomy/fixture_prevalence.svg` | 1 | Synthetic plot. |
| `artifacts/qwen_taxonomy/proposal.json` | 1 | Fixture-derived proposal. |
| `artifacts/qwen_taxonomy/raw_phrase_mappings.csv` | 1 | Fixture-derived mappings. |
| `artifacts/qwen_taxonomy/taxonomy_sensitivity_plan.json` | 1 | Historical sensitivity plan. |
| `artifacts/taxonomy/DECISIONS.md` | 1 | Historical taxonomy notes. |
| `artifacts/taxonomy/approval.json` | 1 | Human approval for one fixture-derived taxonomy digest. |
| `artifacts/taxonomy/categories.csv` | 1 | Fixture-derived taxonomy view. |
| `artifacts/taxonomy/fixture_prevalence.svg` | 1 | Synthetic plot. |
| `artifacts/taxonomy/proposal.json` | 1 | Fixture-derived proposal. |
| `artifacts/taxonomy/raw_phrase_mappings.csv` | 1 | Fixture-derived mappings. |
| `artifacts/taxonomy/taxonomy_sensitivity_plan.json` | 1 | Historical sensitivity plan. |

Totals: 209 files under `artifacts/`, two under `fixtures/`, four under
`hotel_skill_study/`, and one under `tests/`.

## Requirement-by-requirement disposition

“Reuse” below means that the current retained file or a historical algorithmic
idea may be a source for a newly reviewed implementation. It never authorizes
reuse of an old observation or derived output.

| New-study requirement | Recoverable component and finding | Decision |
| --- | --- | --- |
| Time-indexed construct and forbidden interpretations | Both retained docs contain useful disclaimers. The Qwen report language nevertheless organizes change by release generation, and the older docs use terms such as “emergence flags.” | **Revise.** Carry forward only the strict model-association framing and strengthen it everywhere. Do not use emergence or labour-market interpretations. |
| Authenticated local multi-GPU preflight | Old loaders used `device_map="auto"`, sequential model unloading, and recorded a device/runtime subset. The retained Qwen doc admits failed NVML/`nvidia-smi` probes and cache overrides. There is no auditable CUDA/driver/dependency/disk/access matrix, allocation plan, memory feasibility gate, deterministic tolerance, or multi-GPU sharding contract. | **Reject design; revise loader ideas.** Build a fail-closed preflight and explicit GPU ownership/sharding plan. |
| Raw-logit non-instruction primary model panel with publisher-documented periods/cutoffs | `configs/models.json` selects GPT-2 base plus Llama-3-8B-Instruct: instruction status, scale, architecture, tokenizer, and publisher differ. `configs/qwen_models.json` explicitly has unknown cutoffs and selects chat/instruction and differing loader/architecture models ordered by release date. | **Reject for primary.** Research and freeze a new defensible base-model subset. Retain old entries only as leads or explicitly separated sensitivity candidates after re-verification. Never infer cutoff from release. |
| Immutable, complete model metadata | Old configs pin model/tokenizer repository revisions and separate release date from cutoff. They omit or incompletely normalize parameter count, tokenizer identity/version, licenses, weight size (outside Qwen), resolved dtype, runtime requirements, and access-date evidence. URLs were recorded but evidence excerpts/hashes were not frozen. | **Revise.** Reuse the separation and pinning schema concepts, re-research every fact, and add all required fields and evidence snapshots/hashes. |
| Global English construct | Old prompts are English, but scope is one front-desk role and neither document freezes “English-language, globally aggregated associations” as the construct. | **Revise.** State and approve the construct explicitly. |
| Sourced occupational taxonomy across seven required families | `configs/hotel_skill_study.json` contains only `front_desk_employee`. No authoritative occupational source, archived excerpt/hash, role-selection rule, or mapping is present. | **Reject and replace.** Develop an independently sourced role taxonomy covering all required families. |
| Skill vocabulary independent of scored observations | `configs/synonyms.json` contains 13 clusters, while the old pipeline extracted phrases from hand-authored discovery fixtures and proposed/approved categories from those observations. Alias confidence values have no cited derivation; coverage is front-desk-specific. | **Reject as primary vocabulary.** It may inform candidate terminology only. Freeze a new reviewable vocabulary and alternatives before scoring, separately from validation evidence. |
| Controlled sentence-completion prompts, paraphrases, geographic strata, and controls | The retained primary config uses stochastic list generation and Yes/No questions; phrase config has three completion prompts. All are front-desk-only. There are no neutral/geographic strata, positive/negative controls, or full role-by-template design. | **Reject design; revise rendering ideas.** New prompts must end at the phrase slot and be preregistered with strata and controls. |
| Preregistered estimands, exclusions, scoring methods, intervals, stability, and multiplicity | Old code distinguishes total from mean-token probability, calculates within-model ranks, prompt ranges/bootstrap summaries, and labels fixed-prompt resampling descriptively. It lacks the complete primary/secondary estimand set, exclusions, adjacent documented-period contrasts, effect sizes, stability definitions, exact resampling units for every interval, multiplicity families/corrections, and isolated/confounded comparison rules. | **Revise substantially.** Preserve the total/mean distinction and descriptive-resampling warning; write a new locked analysis plan. |
| Digest-bound whole-design review and invalidation | Historical approval checks bind a taxonomy digest and distinguish human from fixture-test scope. They do not bind the model panel, occupations, skills/alternatives, prompts, estimands, exclusions, multiplicity plan, or environment. | **Revise.** Generalize canonical digests and fail-closed approval enforcement to the complete review packet; any design edit invalidates approval. Old approvals are invalid for the new study. |
| Teacher-forced full-vocabulary raw scoring | Historical `_choice_logprob` and phrase scoring retain target token ID/text, raw target logit, full-vocabulary log-sum-exp, reconstructed log probability, totals, and mean-token scores. This directly matches a core mathematical requirement, but phrase boundary handling relies on a configured leading space and old tests do not span the required tokenizer matrix. | **Reuse concept, revise implementation.** Reimplement with explicit boundary contracts, model-specific tokenizer tests, stable precision policy, and schema validation. |
| Content addressing, manifests, fixture separation, atomic writes | Historical code uses canonical JSON SHA-256 identities, pinned revisions, run-identity rejection, temporary-file plus `os.replace`, and fixture labels. It is a single large module and its fixture-derived taxonomy couples test data to design. | **Reuse concepts, revise architecture.** Split schemas/IO/scoring, bind every input digest, fsync where required, and keep all generated runs under the assigned artifacts directory. |
| Failure ledger, missing cells, exact-cell resume, interruption recovery | Old scoring materializes failure records, writes missing-cell inventories, and skips matching content-addressed records. Phrase output has a failure ledger. There is no demonstrated lease/GPU collision protection, intentional-interruption transaction test, or sufficiently strict validation of partially written/cross-run records for the new design. | **Reuse concepts, extend and retest.** Define exact cell identity, atomic state transitions, worker ownership, and recovery semantics. Never silently skip or substitute. |
| GPU-safe sharding | No historical scheduler or explicit device allocation is recoverable; `device_map="auto"` is insufficient for concurrent cells and reproducible sharding. | **Reject and replace.** Implement deterministic worker/GPU assignment and memory admission. |
| Deterministic network-free tests | The removed test file contains useful test intentions: logit/log-sum-exp identity, numerical stability, config pins, approvals/tamper detection, cache resumption, missingness, tokenizer/chat adapter behavior, and artifact validation. Fixtures were also treated as taxonomy inputs and tests depended on tracked generated artifacts. | **Reuse test ideas only.** Write new minimal mathematical/tokenizer doubles and purpose-built fixtures; do not restore historical fixture strings or outputs. Add all newly required boundary, panel, provenance, interruption, and sharding tests. |
| Real-loader/tokenizer smoke tests and deterministic reruns | Retained docs claim Qwen cells ran and an offline cache rerun reused them, but there is no current executable suite or admissible artifact, and primary loaders were not exercised. GPT-2/Llama docs expressly say no real run occurred. | **Reject as verification.** Run new smoke cells for every approved loader/tokenizer and record failures, tolerances, environment, and intentional recovery. |
| Complete primary inference | No historical output uses a comparable non-instruction panel with documented time periods, seven role families, geographic strata, or the approved new skill/prompt design. | **Reject.** Execute from scratch only after whole-design approval. |
| Separated sensitivity inference | Old work includes scoring-method and prompt tables and an unapproved taxonomy alternative, but Qwen mixes unknown cutoff, tokenizer, architecture, scale, and instruction status in one release-generation comparison. | **Reject outputs; reuse separation principle.** New sensitivity namespaces/manifests must isolate or label confounding across prompt, geography, tokenizer, scale, architecture, scoring, role, and skill taxonomies. |
| Multiplicity-adjusted shifts and robustness | Old tables contain ranks, prompt sensitivity, and adjacent-release changes, but no preregistered multiplicity corrections and no admissible adjacent cutoff-period series. | **Reject outputs; reimplement analysis.** Link every estimate to component records and approved design/environment manifests. |
| Reserved authoritative occupational validation | No analytically separate, reserved authoritative validation source or protocol is present. | **Reject and replace.** Freeze external evidence independently and use it only for broad validation. |
| Publication bundle and reproducible report | Historical Qwen `REPORT.md`, tables, plots, manifests, and docs are incomplete for the requested model/data cards, provenance index, checksums, execution log, exact commands, complete limitations, and validated primary/sensitivity designs. They are deleted and inadmissible. | **Reject.** Generate a new validated publication bundle under the assigned artifacts directory. Maintain only source/config/tests/docs in the repository. |

## Component-level reuse register

| Component | Traceable source | Disposition and conditions |
| --- | --- | --- |
| Canonical JSON and SHA-256 identity | `4914bf3^:hotel_skill_study/pipeline.py` | **Reuse after revision.** Specify canonicalization and schema version; hash all design and environment inputs. |
| Atomic JSON write (`mkstemp`/`os.replace`) | Same historical file | **Reuse after revision.** Add durability/error semantics and interruption tests. |
| Run-directory identity rejection | Same historical file | **Reuse after revision.** Extend to approval, model/tokenizer, environment, panel, namespace, and exact cell. |
| Target-logit minus full-vocabulary log-sum-exp | Same historical file and removed math test | **Reuse after revision.** Preserve raw components and test high/low logits and dtype behavior. |
| Total and mean-token phrase scoring | Same historical file; retained `configs/qwen_phrase_scoring.json` | **Reuse after revision.** Make both explicit estimands/sensitivities and enforce tokenizer-boundary checks. |
| Failure materialization and missing-cell inventory | Same historical file; deleted ledgers/tables | **Reuse code pattern only.** New records must be generated by the approved run; deleted ledgers are not imported. |
| Taxonomy digest approval | Same historical file; deleted approval JSON | **Reuse generalized mechanism only.** Old approval records authorize nothing in the new design. |
| Fixed-prompt bootstrap warning | Retained docs and historical summarizer | **Reuse wording principle.** Intervals describe fixed-design sensitivity, never employee, population, training, or labour-market uncertainty. |
| SVG/CSV report writers | Historical monolith | **Revise or replace.** Output format is secondary; validate hashes, record links, missingness, labels, and namespace separation first. |
| GPT-2/Llama config | `configs/models.json` | **Reject as primary.** Heterogeneous base/instruction pair; metadata incomplete. |
| Qwen configs and outputs | `configs/qwen_models.json`, retained Qwen doc, deleted records/report | **Reject as primary and reject old outputs entirely.** Unknown cutoff, chat/instruction status, differing architecture/tokenizer/loader, release-only ordering. Re-researched models could appear only in explicit sensitivity panels. |
| Front-desk config and synonym clusters | `configs/hotel_skill_study.json`, `configs/synonyms.json` | **Reject as frozen design.** May serve as non-authoritative candidate wording during independent taxonomy development. |
| Hand-authored fixtures and fixture-derived taxonomies | Deleted `fixtures/` and taxonomy artifact trees | **Reject and do not restore.** Create fresh, minimal software fixtures that cannot be mistaken for observations. |
| All deleted model records, tables, plots, reports, approvals, and empirical claims | Git objects removed by `4914bf3` | **Reject as evidence and do not restore.** They may be referenced only to explain provenance and design failure. |

## Downstream guardrails

1. The next study must begin with new, source-backed design artifacts, not the
   old taxonomy or results.
2. No run may start until a human approval binds the exact digests of the model
   panel, occupational mapping, skill vocabularies, prompts, estimands,
   exclusions, multiplicity plan, and relevant environment contract.
3. Instruction-tuned, chat, unknown-cutoff, release-ordered, different-scale,
   different-tokenizer, and different-architecture comparisons must remain out
   of the primary temporal series and live in explicit sensitivity namespaces.
4. Generated fixtures, caches, records, logs, reports, plots, validation files,
   and investigation material belong under the run's assigned artifacts
   directory. Historical repository outputs are never copied there as new
   evidence.
5. Every future report must say plainly that findings concern time-indexed
   model associations and do not measure employees, vacancies, regional labour
   markets, causality, or true historical skill demand.
