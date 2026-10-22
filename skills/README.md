# Skills

Reusable know-how any node can be given -- the design agent's own nodes and
every node of a designed agent, on Codex, Claude Code or antigravity alike.

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
| `{python}` | the Python the agent runs with |
| `{artifacts_dir}` | the session's results folder |
| `{workspace}` | the session's project folder |

## Who gets which

Every node has a skills setting, chosen in the UI -- **Configure** for a
designed agent's nodes, the **Design agent** tab for the design agent's own:

- **No skills**
- **Auto-discover** -- the node is shown each skill's name, description and
  folder, and reads the ones that fit its task.
- **Selected skills** -- the ones you tick, given to the node in full.

`suggestions.json` holds the defaults: per design-agent node, and per role in
a designed agent. The designer starts from those and may suggest other skills
for a node; you can change any of it before you approve the design.

Models may read this folder but never change it. Sessions are otherwise
self-contained: this is the only thing outside a session's own folder they are
pointed at.
