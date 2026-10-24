# Skills

Reusable know-how any model call can be given, on Codex, Claude Code or
Antigravity alike, through `research_backend` (see its README, "Skills").

## What is here

| skills | for |
| --- | --- |
| `experiment-design`, `hypothesis-generation`, `statistical-power` | settling what an experiment must show, and how big it must be |
| `research` | papers, citations, datasets and models, with sources |
| `transformers`, `peft`, `bitsandbytes`, `accelerate`, `ml-training-recipes`, `scikit-learn`, `sentence-transformers` | training and fine-tuning: classifiers, LoRA/QLoRA, quantisation, multi-GPU, baselines, embeddings |
| `hf-cli`, `huggingface-datasets`, `hf-mem` | getting models and datasets from the Hub, and whether they fit the GPU |
| `vllm`, `transformer-lens` | batch inference; looking inside a model's representations |
| `lm-evaluation-harness`, `tensorboard` | benchmarks; tracking runs |
| `statistical-analysis`, `scientific-critical-thinking`, `peer-review` | testing results properly; judging claims and the paper |
| `acl-paper`, `ml-paper-writing`, `academic-plotting` | the paper (ACL style files bundled in `ml-paper-writing/templates/acl/`), and its figures |

Where the third-party ones came from, at which commit and under which licence:
`THIRD_PARTY.md`.

## Adding one

Make a folder here with a `SKILL.md` -- the same format Claude Code and
antigravity use:

```
skills/
  my-skill/
    SKILL.md          required
    helper.py         anything else it needs: scripts, templates, data
```

```markdown
---
name: my-skill
description: One or two sentences: what it is for, and when to use it.
---

The instructions themselves, in Markdown.
```

`name` is the folder name. The `description` is what a node sees when it
discovers skills on its own, so say when the skill applies, not only what it
is. It shows up in the UI's skill lists at once; no restart needed.

In the text, these are filled in when the skill is given to a node:

| token | becomes |
| --- | --- |
| `{skill_dir}` | this skill's folder, e.g. to run `{python} {skill_dir}/helper.py` |
| `{skills_dir}` | this folder, to use another skill's files |
| `{python}` | the Python the program runs with |
| `{workspace}` | the session's project folder |

## Who gets which

Each `research_backend` session chooses:

- **No skills** (`skills=None`)
- **Auto-discover** (`skills="auto"`) -- the model is shown each skill's name,
  description and `SKILL.md` path, and reads the ones that fit its task.
- **Selected skills** (`skills=["acl-paper", ...]`) -- given in full.

```python
import research_backend as RB
RB.list_skills("skills")
RB(provider="codex", skills_dir="skills", skills="auto")
```

Models may read this folder but never change it.
