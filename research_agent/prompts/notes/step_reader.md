<!-- In the designer's message: each step is a subagent with its own context, and what that
     context must hold. $run: the run folder. -->
Code builds each step's subagent from the step, and tells it this and nothing else:

  You are <the owner node's name>.
  The work: <the step's `detail`>
  Done when: <the step's `check`>
  How your work is checked: the criteria assessed at this step, in words
  Your inputs: each earlier step it depends on, by title, with its folder and
               the files that step reported
  Where to work: its outputs folder, the code folder, the study's Python, and a
                 command to launch GPU jobs

The folders are real, so name files in them:
  step N writes to   $run/artifacts/step_NN/   (step 3: step_03, step 12: step_12)
  code goes in       $run/work/
  the run folder     $run
  when the execution started, and the user's go that started it:
                     $run/execution/execution_start.json (code writes it)
Approval, the go, budgets and time limits, the orchestrator and the validator
are code's: a step neither checks nor records them.

So the step's `detail` must give its subagent, written in:
- what to do, and in a sentence why the study needs it;
- what it reads: earlier steps' files by title and path
  ($run/artifacts/step_03/train.jsonl), and outside sources by full path, id
  and version (datasets, models, URLs);
- every fixed value it must keep: label sets, splits, seeds, prompts,
  hyperparameters, metrics with their formulas, compute and time limits --
  the values themselves, copied from the protocol, not "as specified";
- what each term, model or condition is, the first time it appears;
- what it writes: file names in its own folder, and what each holds;
- what it must not do (look at test labels, change earlier steps' files);
and its `check`: a test it can run on its own files.

And nothing that is not its business: other steps' work, criteria checked at
other steps, the hypotheses, approval, the orchestrator and the validator --
code runs those. Before you answer, read each step as its subagent: could it do
the work, and know it is done, without asking one question? A reader model
does exactly that for every step, and its questions come back to you.
