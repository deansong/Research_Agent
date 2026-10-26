---
name: research
description: Look things up in the literature, in every field, and on the Hugging Face Hub: papers from OpenAlex, Semantic Scholar, PubMed, Europe PMC, arXiv and Hugging Face; the papers of a venue -- ACL Anthology (ACL, EMNLP, NAACL, Findings, TACL...), NeurIPS, ICLR, ICML, AAAI -- by topic, year and track; one paper's abstract and links by arXiv id, ACL id, DOI or PMID; downloading a paper's PDF and text; citation counts; datasets; models. Every finding with its source.
---

RESEARCH SKILLS -- tools you run as shell commands. Each prints plain text
(add --json for JSON, --limit N for more rows, --since YEAR for recent work).
Prefer them to guessing.

FINDING PAPERS
  {python} {skill_dir}/research_tools.py search    "<words>"   every field: OpenAlex (+ Semantic Scholar), merged
  {python} {skill_dir}/research_tools.py search    "<words>" --sources openalex,pubmed,europepmc
  {python} {skill_dir}/research_tools.py pubmed    "<words>"   biomedicine and clinical work (PubMed)
  {python} {skill_dir}/research_tools.py europepmc "<words>"   life sciences, with preprints and open full text
  {python} {skill_dir}/research_tools.py papers    "<words>"   ML papers with arXiv ids (Hugging Face; fast)
  {python} {skill_dir}/research_tools.py arxiv     "<words>"   arXiv's own search (slow; if papers misses)
  {python} {skill_dir}/research_tools.py openalex  "<words>"   one source alone; also s2 (needs its key)
  {python} {skill_dir}/research_tools.py scholar   "<words>"   Google Scholar via SerpAPI: needs SERPAPI_API_KEY,
                                                             and each search costs a credit -- use it when the
                                                             others miss something

THE PAPERS OF A VENUE -- from the venue's own records (first use downloads and caches them)
  {python} {skill_dir}/research_tools.py venue "<topic>" --venue acl,emnlp,naacl --since 2023
  {python} {skill_dir}/research_tools.py venue "<topic>" --venue acl --year 2024 --kind long,findings
  {python} {skill_dir}/research_tools.py venue "<topic>" --venue neurips,iclr --track "<area words>"
  {python} {skill_dir}/research_tools.py venue "<topic>" --venue icml --since 2024
  {python} {skill_dir}/research_tools.py venue "<topic>" --venue aaai --track "natural language"
  {python} {skill_dir}/research_tools.py tracks "<words>" --venue aaai      the tracks there are
  venues: acl emnlp naacl eacl coling aacl findings tacl cl conll lrec workshop anthology (all of
  the ACL Anthology), neurips, iclr, icml, aaai. Tracks: AAAI has them; NeurIPS and ICLR give each
  paper's primary area (2024 on); the ACL Anthology records none -- use --kind (long, short,
  findings, demo, industry, srw) and put the area's words in the topic.
  Use it for "what was published at <venue> on <topic>": the venue's own list, accepted papers
  only, each with its abstract -- then read the promising ones.

READING A PAPER
  {python} {skill_dir}/research_tools.py download <id> --text   PDF and its text into ./papers
  <id>: an ACL id (2024.acl-long.1), arXiv id, DOI, PMLR page, or a PDF URL. OpenReview refuses
  downloads by script: use the paper's arXiv version. Read the .txt for methods and numbers.

ONE PAPER, AND WHO CITES IT -- by arXiv id, ACL id, DOI, PMID, OpenAlex id (W...) or Semantic Scholar id
  {python} {skill_dir}/research_tools.py paper  <id>    full abstract, ids, venue, open-access link
  {python} {skill_dir}/research_tools.py cites  <id>    how often cited, and the most-cited citing works

RESOURCES
  {python} {skill_dir}/research_tools.py datasets "<words>"   Hugging Face datasets: id, size, licence
  {python} {skill_dir}/research_tools.py models   "<words>"   Hugging Face models: id, task, licence

WHICH SEARCH
- Start with `search`: OpenAlex covers every field and publisher (journals,
  conferences, preprints). Semantic Scholar is added by itself when its key
  is set (SEMANTIC_SCHOLAR_API_KEY). Google Scholar is never added by itself:
  ask it by name, sparingly. A source that does not answer, or is not asked,
  is named in a last "note" row -- carry on with what came back.
- Biomedical or clinical questions: add pubmed (and europepmc for
  preprints): `search "<words>" --sources openalex,pubmed,europepmc`.
- Machine learning: `papers` finds arXiv papers fastest; `search` finds their
  published versions and the work outside arXiv.
- Recent state of the art: add --since (e.g. --since 2024).

Your own web search, if you have one, is for what these do not cover:
leaderboards, blog posts, library documentation, hardware requirements.

HOW TO SEARCH -- separate searches for separate questions
1. Landscape: the task's current state of the art -- recent surveys, the
   benchmarks people report on and the best numbers, the leading methods.
   Use --since for recency; collect the field's terms and synonyms.
2. Closest prior work: the idea itself and its near neighbours, under every
   name it may go by. A novelty search is its own search, not a side effect
   of the landscape one.
3. Rivals and contrary results: for each candidate method or explanation,
   search for failures, null results, critiques, reproductions and known
   confounds (contamination, leakage, shortcuts, evaluation flaws).
4. Measurement: how the constructs and metrics were operationalised and
   validated; the standard datasets, splits and baselines.
5. Citations both ways: `cites <id>` for who built on a key paper (forward);
   its reference list for what it built on (backward). Recent is not the
   same as relevant -- foundational work counts.
Prefer the primary source: the paper with the method or number, not a survey
quoting it. Label preprints as preprints. Check the published version of an
arXiv paper when one exists (`search` finds both).

WHAT YOU SEARCHED IS PART OF THE ANSWER
Say which sources and queries you used and the date range. "Not found in
these searches" is not "does not exist": never claim novelty from an empty
search -- say what was searched and what came closest.

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
   hyperparameters, what one run costs.
4. Stop when the questions are answered. A few precise findings beat many
   loose ones.
