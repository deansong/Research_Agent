# Invalidated approval provenance

The file `unsupported_human_approval.json` asserted a human approval even though
no human answer had been supplied. It is not valid authorization and has been
removed from the active taxonomy directory.

`qwen_fixture_score_run_invalid_human_approval/` is the clearly synthetic 84-cell
run derived from that unsupported record. Its numerical contents are still only
software fixtures, but the run is excluded because its approval provenance is
invalid. It is retained here rather than deleted so the failure remains auditable.

The active proposal remains unchanged at digest
`3095201c9ad6c3243f2a60a63dc4959e4d3252a54a4f8b7af3f6305a55ca03cc`.
