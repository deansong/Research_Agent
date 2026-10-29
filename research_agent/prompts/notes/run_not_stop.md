<!-- Sent back once to the execution orchestrator when it stops while a step is open and untried.
     $step: the first such step. -->
`stop` ends the execution, and step $step is open to run and untried. The user started the execution: they want the steps run. Stop only when going on would be wrong whatever the user said before starting (the budget, a step that cannot succeed); otherwise run step $step, or ask the user if the state leaves a real decision open.
