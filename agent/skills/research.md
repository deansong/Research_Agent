RESEARCH SKILLS -- tools you run as shell commands. Each prints plain text
(add --json for JSON, --limit N for more rows). Prefer them to guessing.

  {tools} papers   "<words>"     papers on a topic, with arXiv ids (fast)
  {tools} arxiv    "<words>"     arXiv's own search (slow; use if papers misses)
  {tools} paper    <arxiv id>    one paper: full abstract, code link, URLs
  {tools} cites    <arxiv id>    how often cited, and the top citing works
  {tools} datasets "<words>"     Hugging Face datasets: id, size, licence
  {tools} models   "<words>"     Hugging Face models: id, task, licence

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
