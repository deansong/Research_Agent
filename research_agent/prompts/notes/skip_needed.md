<!-- Sent back to the execution orchestrator when it skips a step that later steps need.
     $step: the step; $needed: the steps not settled yet that need it. -->
skip $step: steps $needed need it, so skipping it gives up what they would deliver. Only the user decides that. Use `ask_user` with step $step: say what steps $needed would lose, and the options -- raise the limit that stopped it (the user's to raise), drop that work, or revise the design.
