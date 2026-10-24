---
name: research-orchestration
description: How to coordinate a research team before any experiment runs -- choosing the next step (research, revise, ask, finish), checking work is ready before asking the user to approve it, and keeping the shared research state short and current. For the orchestrator of a gated, user-approved research design.
---

# Research orchestration

You decide what the team does next. Aim for the fewest steps that give the
user something good to approve; every step costs a model call and the
user's attention.

## Choosing the next step

Go down this list and take the first that applies:

1. **The user asked for something** (requests from the discussor): do it,
   unless the state shows it cannot be done yet -- then say why and ask.
2. **A decision only the user can make is missing** -- the goal, or a
   constraint (compute, budget, deadline, data, models): ask_user, all
   questions in one message, each with your recommended default.
3. **Research would change the design**: research, with precise questions.
   Only when the answer could change a hypothesis, the rubric, the protocol
   or the plan. Never ask again what the evidence already answers. Stop when
   another round would only add confirmation. The first round on a topic is a
   landscape scan -- the current state of the art, what leads recent
   benchmarks -- not a survey of approaches someone already assumed.
4. **Hypotheses, rubric and protocol** are missing or fail the readiness
   check below: hypothesize, naming exactly what fails. Ready but not
   approved: ask_user to approve them.
5. Then the **plan** (approve), then the **design** (approve), then ready.

After new evidence, pick a direction (the outer loop of Orchestra Research's
autoresearch, kept to before-the-experiment):

- **Deepen** -- the evidence fits a hypothesis but leaves its size or
  mechanism open: sharpen the hypothesis.
- **Broaden** -- an adjacent question would make the study clearly more
  informative within the constraints: ask the user before adding it.
- **Pivot** -- a core assumption is false, or the question is already
  answered (novelty): tell the user and propose the alternative.
- **Conclude** -- the design is approved: ready. Carrying the plan out
  (sign-offs, licences, environments, running steps) is the execution
  stage's work; record such items as open questions, do not pursue them.

## Readiness check: before asking the user to approve

- **Hypotheses**: each has a falsifiable prediction with a direction and a
  size; cites evidence ids, verified where possible; considers a rival
  explanation; is testable within the constraints.
- **Rubric**: every criterion has a measure and a pass threshold, and names
  the hypotheses or steps it applies to.
- **Protocol**: baselines, metrics, statistical test, seeds or repetitions
  and stop criteria are fixed before any result; the sample size is
  justified (a power analysis or a precedent).
- **Plan**: every step has a check; dependencies make sense; the total cost
  fits the constraints; a gate stands before each expensive step.
- **Design**: every plan step has exactly one node; one read-only validator.
- **Claims**: none stronger than its evidence; unverified sources say so.

Anything that fails goes back to the node that made it, with the failure
named. For a deeper review, read and apply:

- `{skills_dir}/scientific-critical-thinking/SKILL.md` -- validity, bias,
  confounders, how strong the evidence is;
- `{skills_dir}/peer-review/SKILL.md` -- reviewing a research proposal as a
  whole.

## Keeping the state clean

The state is every node's context. Keep it short and current:

- Before adding an open question or a decision, check the state: if it is
  there already, in any wording, do not add it again.
- One precise item beats several overlapping ones.
- A decision says what, why, and when to revisit it ("revisit if the pilot
  shows ..."). The method: `{skills_dir}/scientific-brainstorming/SKILL.md`,
  "Decide and log".
- `summary` says where the study stands in three to five sentences: what is
  settled, what is waiting on whom, what comes next.

## Asking the user

`note_for_discussor` holds what the discussor must ask: each decision, its
options, and your recommendation. Never ask what the state already answers.
