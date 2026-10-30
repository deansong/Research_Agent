<!-- In the designer's message: each step is its worker's whole prompt, so it must stand on its own. -->
Code turns each step into its worker's whole prompt -- this, and nothing else:

  You are <the owner node's name>.
  The work: <the step's `detail`>
  Done when: <the step's `check`>
  How your work is checked: <the criteria assessed at this step, in words>
  What the study needs from this step: <a short note the orchestrator adds>
  Your inputs: each earlier step it depends on, by title, with its folder and files
  Where to work: its folders, and how to launch long jobs

The worker never sees the brief, the hypotheses, the protocol, the other steps
or this design's names for things. So write each step to stand on its own: a
researcher new to this study, given only this prompt and its input files, must
be able to do the work and know it is done without asking a single question.
Anything they would have to ask -- what something is, which setting, where an
earlier result lives, what counts as done -- is missing from the step.
