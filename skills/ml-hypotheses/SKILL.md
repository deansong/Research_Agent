---
name: ml-hypotheses
description: Turning a research question in NLP, ML, AI or computational social science into testable hypotheses -- the claim type, the rival explanations an ML result must rule out (leakage, contamination, tuning budget, seeds, prompts, metric artifacts), predictions that tell them apart, and how each construct is measured. For writing or revising hypotheses, the rubric and the evaluation protocol.
---

HYPOTHESES FOR NLP / ML / AI / COMPUTATIONAL SOCIAL SCIENCE
A hypothesis is a candidate to be tested, never a finding. Each one needs a
claim, a prediction that could fail, the evidence it rests on, and the rivals
it must beat.

1. FRAME THE QUESTION -- the frame that fits, not a medical one
   - Method / model:  task, data (+ split), system vs. named baselines,
     metric, budget (compute, data, tuning).
     "On <task/data>, <method> beats <baselines> on <metric> by >= <margin>
      under <equal budget>."
   - Analysis / mechanism: model, phenomenon, probe or intervention
     (ablation, patching, controlled input change), observable.
   - Evaluation / benchmark: what the benchmark should measure, what it
     measures instead, the evidence that would tell them apart.
   - Computational social science: population and platform, construct,
     measure (and its validation), comparison or variation, period.
   Say what answer would change what people do or believe.

2. NAME THE CLAIM TYPE -- it decides the design
   comparative (A > B), ablation (component X causes gain), scaling (trend
   with size/data/compute), generalisation (holds out of distribution, across
   languages or domains), robustness (holds under perturbation), mechanistic
   (the model does it this way), measurement (the metric or classifier
   captures the construct), descriptive or associational (CSS: who, what,
   how much), causal (CSS: needs an identification strategy).
   Do not claim more than the type allows: a benchmark gain is not
   "understanding"; a correlation in social data is not an effect.

3. GENERATE RIVALS -- what else would produce the same result
   - Leakage or contamination: test items in pretraining or prompts,
     labels leaking through features or splits (arXiv:2207.07048).
   - Unequal budget: the method tuned harder, trained longer or given more
     data than baselines (arXiv:1707.05589); baselines not current.
   - Noise: seeds, test-set size, prompt wording (arXiv:2310.11324) -- see
     the ml-statistics skill.
   - Artifacts: the dataset solvable by shortcuts (arXiv:1803.02324); the
     metric rewarding length, format or style rather than the skill.
   - Judge effects: an LLM judge preferring its own family, length or
     position.
   - Selection: best-of-many runs or prompts reported; cherry-picked subsets.
   - CSS: sampling and platform effects, bots and duplicates, confounding,
     reverse causation, a classifier's errors correlated with the groups
     compared.
   Keep the rivals that are plausible here, and say which experiment or
   control rules each out.

4. PREDICTIONS THAT CAN FAIL
   For each hypothesis: the observable, the expected direction and the
   smallest difference that would count, and the result that would refute
   it. Prefer tests where the hypothesis and its rivals predict different
   things (e.g. a gain that survives decontaminated or newly written test
   items; an ablation that removes the gain; a control task the method
   should not help).

5. MEASURE WHAT YOU CLAIM
   - Benchmarks and metrics: does the metric reflect the construct? Use the
     standard metric for comparability and, where it is weak, a second one.
   - Human evaluation: who judges, instructions, how many items, agreement.
   - LLM-as-judge: validate against people on a sample; randomise order.
   - CSS: validate classifiers and dictionaries on hand-labelled target data;
     state the construct's definition (doi:10.1145/3442188.3445901).

6. THE RUBRIC AND PROTOCOL -- fixed before results
   Each rubric criterion: a measure and a threshold, tied to hypothesis ids
   ("H1 supported if mean accuracy gain >= 2 points over the strongest
   baseline, 5 seeds, paired bootstrap p < .05"). The protocol: baselines
   (named, current, equally tuned), metrics, data and splits, seeds or
   repetitions, the statistical test (ml-statistics), what is primary and
   what exploratory, and when to stop.

7. NOVELTY AND EVIDENCE
   Cite what each hypothesis rests on by id. "Not found in the searches
   made" is not "new": say what was searched. A hypothesis already tested
   elsewhere is still worth testing when the conditions differ -- say how.

8. ETHICS, WHERE IT APPLIES
   Human annotators or participants (consent, pay, review board where
   required); personal or social-media data (terms of service, privacy,
   de-identification); dual-use or harmful-capability work. Name the issue
   and the step it needs; do not let it block unrelated work.

Write few, sharp hypotheses: each with its rivals and its deciding
experiment. Drop or revise ones the evidence has already settled.
