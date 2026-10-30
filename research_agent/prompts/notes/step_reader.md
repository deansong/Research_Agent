<!-- In the designer's message: who reads a step, so it writes each one for that reader. -->
Each step is done by a model that sees only this, built by code from the step:

  You are <the owner node's name>.
  The work: <the step's `detail`>
  Done when: <the step's `check`>
  How your work is checked: <the criteria assessed at this step, in words>
  What the study needs from this step: <a short note the orchestrator adds>
  Your inputs: each earlier step it depends on, by title, with its folder and files
  Where to work: its folders, and how to launch long jobs

It never sees the brief, the protocol, the hypotheses, the other steps' details,
or its node's instructions. So write each step's `detail` and `check` to be done
from that alone:
- name every model, dataset, condition and setting in full the first time
  ("the original Qwen3-4B-Instruct checkpoint, no fine-tuning"), never only a
  label like "A" or "condition B";
- say what earlier work it uses by that step's title and file, never "step 2";
- write in the values from the protocol it must keep (seeds, splits, metrics,
  limits); `follows` only names which parts those come from;
- one deliverable and one check, in plain words.
