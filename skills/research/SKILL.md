---
name: research
description: Look things up in the literature and on the Hugging Face Hub: papers (arXiv, Hugging Face), one paper's abstract and code, citation counts, datasets, models. Every finding with its source.
---

RESEARCH SKILLS -- tools you run as shell commands. Each prints plain text
(add --json for JSON, --limit N for more rows). Prefer them to guessing.

  {python} {skill_dir}/research_tools.py papers   "<words>"     papers on a topic, with arXiv ids (fast)
  {python} {skill_dir}/research_tools.py arxiv    "<words>"     arXiv's own search (slow; use if papers misses)
  {python} {skill_dir}/research_tools.py paper    <arxiv id>    one paper: full abstract, code link, URLs
  {python} {skill_dir}/research_tools.py cites    <arxiv id>    how often cited, and the top citing works
  {python} {skill_dir}/research_tools.py datasets "<words>"     Hugging Face datasets: id, size, licence
  {python} {skill_dir}/research_tools.py models   "<words>"     Hugging Face models: id, task, licence

Your own web search, if you have one, is for what these do not cover:
leaderboards, blog posts, library documentation, hardware requirements.

METHOD
1. Search for each question. Read the abstracts (`paper`) of the few that
   matter; open `full_text` only for a number you need from inside a paper.
2. Every claim you report names its source: an arXiv id, a dataset or model
   id, or a URL. A claim without a source is a guess -- leave it out, or say
   plainly that you could not find it.
3. Prefer what decides the experiment: the standard datasets and splits, the
   baselines people compare against and the numbers they get, the usual
   hyperparameters, what one run costs. Skip history and surveys.
4. Stop when the questions are answered. A few precise findings beat many
   loose ones.
