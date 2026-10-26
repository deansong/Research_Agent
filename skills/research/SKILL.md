---
name: research
description: Look things up in the literature, in every field, and on the Hugging Face Hub: papers from OpenAlex, Semantic Scholar, PubMed, Europe PMC, arXiv and Hugging Face; one paper's abstract and links by arXiv id, DOI or PMID; citation counts and citing works; datasets; models. Every finding with its source.
---

RESEARCH SKILLS -- tools you run as shell commands. Each prints plain text
(add --json for JSON, --limit N for more rows, --since YEAR for recent work).
Prefer them to guessing.

FINDING PAPERS
  {python} {skill_dir}/research_tools.py search    "<words>"   every field: OpenAlex + Semantic Scholar, merged
  {python} {skill_dir}/research_tools.py search    "<words>" --sources openalex,pubmed,europepmc
  {python} {skill_dir}/research_tools.py pubmed    "<words>"   biomedicine and clinical work (PubMed)
  {python} {skill_dir}/research_tools.py europepmc "<words>"   life sciences, with preprints and open full text
  {python} {skill_dir}/research_tools.py papers    "<words>"   ML papers with arXiv ids (Hugging Face; fast)
  {python} {skill_dir}/research_tools.py arxiv     "<words>"   arXiv's own search (slow; if papers misses)
  {python} {skill_dir}/research_tools.py openalex  "<words>"   | s2 "<words>"   one source alone
  {python} {skill_dir}/research_tools.py scholar   "<words>"   Google Scholar, only if SERPAPI_API_KEY is set

ONE PAPER, AND WHO CITES IT -- by arXiv id, DOI, PMID, OpenAlex id (W...) or Semantic Scholar id
  {python} {skill_dir}/research_tools.py paper  <id>    full abstract, ids, venue, open-access link
  {python} {skill_dir}/research_tools.py cites  <id>    how often cited, and the most-cited citing works

RESOURCES
  {python} {skill_dir}/research_tools.py datasets "<words>"   Hugging Face datasets: id, size, licence
  {python} {skill_dir}/research_tools.py models   "<words>"   Hugging Face models: id, task, licence

WHICH SEARCH
- Start with `search`: OpenAlex covers every field and publisher (journals,
  conferences, preprints); Semantic Scholar adds CS coverage when it answers.
  A source that does not answer is named in a last "note" row -- carry on
  with what came back.
- Biomedical or clinical questions: add pubmed (and europepmc for
  preprints): `search "<words>" --sources openalex,pubmed,europepmc`.
- Machine learning: `papers` finds arXiv papers fastest; `search` finds their
  published versions and the work outside arXiv.
- Recent state of the art: add --since (e.g. --since 2024).

Your own web search, if you have one, is for what these do not cover:
leaderboards, blog posts, library documentation, hardware requirements.

METHOD
1. Search for each question. Read the abstracts (`paper`) of the few that
   matter; open the full text (`full_text`, `open_access`) only for a number
   you need from inside a paper.
2. Every claim you report names its source: an arXiv id, a DOI, a PMID, a
   dataset or model id, or a URL. A claim without a source is a guess --
   leave it out, or say plainly that you could not find it. Sources are
   checked automatically; invented ones are flagged.
3. Prefer what decides the experiment: the standard datasets and splits, the
   baselines people compare against and the numbers they get, the usual
   hyperparameters, what one run costs. Skip history and surveys.
4. Stop when the questions are answered. A few precise findings beat many
   loose ones.
