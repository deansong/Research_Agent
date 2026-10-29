# Third-party material

- **Skills:** see `skills/THIRD_PARTY.md`.
- **Refinement prompts and method:** `research_agent/prompts/refine/*.md`
  adapt prompt text from [google-research/harness2](https://github.com/google-research/harness2)
  (`harness2/core/prompts.py`: `_INV_TEMPLATE`, `_DOM_TEMPLATE`, `GENERALIZE`,
  `select`, `experience`, `aggregate` / `compose_round`), at commit `1cfe254e8d25`, licensed
  Apache-2.0, and `research_agent/refine/` reimplements its
  Propose-Probe-Compose loop with an experience library. No harness2 code is
  included. Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
