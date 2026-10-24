---
name: experiment-design
description: The checklist of what an experiment plan needs settled -- claim, code, design, run, analysis, checks, gates -- with a suggested answer for each open item. For discussing and planning an experiment.
---

EXPERIMENT DESIGN CHECKLIST -- what a plan needs settled. Ask only what is
still open and would change the work; suggest a specific answer for each.

0. Claim      What result supports it, what refutes it? ("LoRA matches full
              fine-tuning within 1 point on X", not "improve the model".)
1. Code       What must be written, by kind: data loading, the method, each
              baseline (named). What exists already. The environment:
              anything to install or build first.
2. Design     Dataset and split; model and where its weights come from;
              hyperparameters and how they are chosen; ablations IN and OUT;
              seeds. Pedantic here: "a few configurations" never finishes.
3. Run        Compute: hardware, time for ONE run, total budget; which runs,
              in what order.
4. Analysis   The metric that settles it, the difference worth believing,
              the deliverable (table, figure, written finding, merged change).
5. Checks     How to tell each piece worked: a test, a number in a range, a
              file that exists.
6. Gates      What a person must approve before the run goes on. Default:
              none -- a gate stops everything until somebody comes back.

Not research (a refactor, a tool, a bug)? Ask about the work instead; 1, 5
and 6 still apply.
