<!-- Sent back once to the execution orchestrator when it asks the user about a step it can retry.
     $step: the step id. -->
step $step can be retried: its owner works with full access (files, commands, network, GPUs) -- what an earlier attempt or a read-only sandbox could not reach says nothing about this one. Retry it with a `task` saying what to do differently, or ask the user only for a decision the state does not settle.
