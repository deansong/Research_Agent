---
name: ml-statistics
description: How big an ML / NLP / AI experiment must be, and how to tell a real difference from noise -- test-set size, seeds, LLM-judge and human-preference sample sizes, paired tests, several benchmarks at once, annotator agreement, and computational-social-science samples. For planning hypotheses, protocols and analyses before anything runs.
---

STATISTICS FOR ML / NLP / AI EXPERIMENTS -- what makes a difference believable.
Compute, do not guess: `{python} {skill_dir}/ml_power.py` (standard library).

WHERE THE NOISE COMES FROM -- name each one the claim must beat
- Test items: a finite test set samples the task. 500 items at 80% accuracy
  cannot show a 2-point gap; a paired test on the same items is what to use.
- Seeds and training randomness: initialisation, data order, dropout. Fine-
  tuning results move by points across seeds (arXiv:2002.06305); report the
  spread, never one run.
- Prompts and decoding: for LLMs, wording, format, examples and temperature
  move scores by many points (arXiv:2310.11324). Fix them before results, or
  sample over them and report the spread.
- Judges: an LLM-as-judge or human preference is a noisy measurement with its
  own biases (position, length, self-preference). Validate it against people
  on a sample, randomise positions, report agreement.
- Tuning budget: the method tuned harder than the baselines wins for that
  reason alone (arXiv:1707.05589). Give every system the same budget.

HOW BIG -- run the numbers for the protocol
  ml_power.py accuracy --n 500 --acc 0.80              smallest gap a paired test detects on 500 items
  ml_power.py accuracy --gap 0.02 --acc 0.80 --disagree 0.10   items needed (disagree: P(only one is right))
  ml_power.py seeds --sd 0.8 --gap 1.0                 runs per system for a 1-point gap, seed spread 0.8
  ml_power.py seeds --sd 0.8 --runs 5                  smallest gap 5 runs each can show
  ml_power.py winrate --halfwidth 0.05                 judged pairs for a +-5-point win rate
  ml_power.py winrate --wins 130 --n 200               a win rate with its interval
Defaults alpha 0.05 two-sided, power 0.8. Without a measured disagreement rate
the script assumes independent errors, which is conservative. Most NLP
comparisons are underpowered (arXiv:2010.06595): if the numbers say the test
set cannot show the gap you expect, change the claim, the data or the
budget -- before anything runs.

THE PROTOCOL MUST FIX, BEFORE RESULTS
1. The primary metric and the comparison that settles each hypothesis, and
   the smallest difference that would matter (not "any improvement").
2. The unit: test items (paired across systems), seeds (independent runs),
   prompts, or annotators -- and which noise source the interval covers.
3. The test: paired bootstrap or approximate randomisation over items
   (`ml_power.py bootstrap a.txt b.txt` once results exist); McNemar for
   accuracy; across seeds, compare distributions (mean and spread, or almost
   stochastic order, ACL 2019 doi:10.18653/v1/P19-1266) rather than best runs.
   Choosing among them: doi:10.18653/v1/P18-1128.
4. Several datasets, metrics or baselines: correct for it
   (`ml_power.py holm p1 p2 ...`), or name one primary comparison and call
   the rest exploratory.
5. Splits: the test set untouched until the end; selection on a dev set.
   Check for contamination and leakage (a pretrained model may have seen the
   benchmark; features may leak labels -- arXiv:2207.07048). Standard splits
   can mislead; random or new splits test it (doi:10.18653/v1/P19-1267).
6. Report: mean and spread over seeds, intervals, the number of runs, and
   every run made -- not the best one.

LLM EVALUATIONS
Treat each eval question as a sample: report a standard error or interval
for every score, cluster it when questions share a passage or template, and
compare models paired on the same questions (arXiv:2411.00640).

ANNOTATION AND HUMAN JUDGEMENTS
Report agreement (Krippendorff's alpha or Cohen's kappa) on a doubly-annotated
sample before trusting labels; annotation artifacts can make a dataset
solvable without the skill it claims to test (arXiv:1803.02324).

COMPUTATIONAL SOCIAL SCIENCE
- The unit is a person, account, document or community -- not a token.
  Posts by one account are not independent: cluster by author.
- Say whom the sample represents (platform, period, language, how collected)
  and what it cannot generalise to.
- A classifier's output is a measurement: validate it against hand labels on
  the target data before using it as a variable, and carry its error into the
  analysis. Construct validity: does the measure capture the concept
  (doi:10.1145/3442188.3445901)?
- Regression on observational data is associational; a causal claim needs a
  design (an experiment, a natural experiment, a stated identification
  strategy).
- For survey-style or regression power with human participants, the
  `statistical-power` skill's scripts cover the classic tests.
