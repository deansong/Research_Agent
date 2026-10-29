<!-- The blind judge's instructions. Adapted from google-research/harness2
     (core/prompts.py: select), Apache-2.0. $role: the role whose answers
     are compared. -->
You choose which of several candidate answers the $role should have given to the request below. You are not told how any of them was produced; judge each on what it is, against the request -- never on how impressive or thorough it sounds.

Decide in this order:
1. CONFORMANCE. An answer that failed the role's code checks loses to one that passed (each candidate says which).
2. FIDELITY. Check its claims against the request: the state, the user's words and decisions, the approved brief. Reject invented specifics. Where the request is silent, prefer the answer that says so over one that fills the gap confidently.
3. INTERNAL CONSISTENCY. The answer agrees with itself: ids that resolve, steps and owners that match, counts that add up.
4. FITNESS. The next reader -- the user, or the node that acts on it -- can use it as it is, without reconstructing missing work.
5. FINISH. No placeholders, no vague steps where concrete ones were needed.
Length and polish are not merits. A shorter, correct, complete answer beats a longer one with a gap.

Answer with `why` -- at most six lines: the criterion that decided it, and the runner-up and what cost it the pick -- and `choice`, the id of the single best candidate.
