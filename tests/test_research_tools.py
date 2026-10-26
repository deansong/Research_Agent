"""The research skill's tool, offline: ids, merging several sources, and the
checking of a researcher's sources (arXiv ids, DOIs, PMIDs)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from research_agent.bootstrap import verify

TOOL = Path(__file__).resolve().parents[1] / "skills" / "research" / "research_tools.py"


@pytest.fixture(scope="module")
def tools():
    spec = importlib.util.spec_from_file_location("research_tools", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ids_of_every_kind(tools):
    assert tools.kind_of("2106.09685v2") == ("arxiv", "2106.09685")
    assert tools.kind_of("arXiv:2106.09685") == ("arxiv", "2106.09685")
    assert tools.kind_of("https://doi.org/10.1038/s41586-021-03819-2") == ("doi", "10.1038/s41586-021-03819-2")
    assert tools.kind_of("PMID: 40418571") == ("pmid", "40418571")
    assert tools.kind_of("https://pubmed.ncbi.nlm.nih.gov/40418571/") == ("pmid", "40418571")
    assert tools.kind_of("W3177828909") == ("openalex", "W3177828909")
    assert tools.kind_of("a" * 40) == ("s2", "a" * 40)
    with pytest.raises(SystemExit):
        tools.kind_of("not an id")


def test_openalex_abstracts_are_put_back_together(tools):
    assert tools._abstract({"world": [1], "hello": [0], "again": [2]}) == "hello world again"
    assert tools._abstract(None) == ""


def test_search_merges_sources_and_says_which_failed(tools, monkeypatch):
    one = [{"title": "LoRA: Low-Rank Adaptation", "doi": "10.48550/arXiv.2106.09685", "arxiv": "2106.09685",
            "year": 2021, "cited_by": None},
           {"title": "Only in OpenAlex", "doi": "10.1/x"}]
    two = [{"title": "LoRA: low-rank adaptation", "arxiv": "2106.09685", "cited_by": 9000, "s2": "abc"}]

    def broken(*a, **k):
        raise SystemExit("https://x.org/q: HTTP 429 -- rate limited")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    monkeypatch.setitem(tools.SOURCES, "openalex", lambda q, n, since=None: one)
    monkeypatch.setitem(tools.SOURCES, "pubmed", lambda q, n, since=None: [])
    monkeypatch.setitem(tools.SOURCES, "europepmc", broken)
    monkeypatch.setattr(tools, "s2", lambda q, n, since=None, patient=True: two)
    rows = tools.search("lora", 5, None, ("openalex", "s2", "pubmed", "europepmc"))
    assert rows[0]["found_in"] == "openalex, s2", "the same paper twice is one row"
    assert rows[0]["cited_by"] == 9000 and rows[0]["s2"] == "abc", "its fields put together"
    assert rows[1]["title"] == "Only in OpenAlex"
    assert rows[-1] == {"note": "not answered -- europepmc: HTTP 429 -- rate limited"}


def test_scholar_needs_a_key(tools, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="SERPAPI_API_KEY"):
        tools.scholar("lora", 3)


def test_keys_never_printed(tools):
    assert tools._safe("https://a.org/x?q=1&api_key=SECRET&b=2") == "https://a.org/x?q=1&api_key=***&b=2"


def test_sources_checked_by_doi_pmid_or_arxiv():
    assert verify.paper_id("Jumper et al., Nature, doi:10.1038/s41586-021-03819-2.") == \
        "10.1038/s41586-021-03819-2"
    assert verify.paper_id("https://doi.org/10.48550/arXiv.2106.09685") == "2106.09685"
    assert verify.paper_id("PMID: 40418571") == "PMID:40418571"
    assert verify.paper_id("https://pubmed.ncbi.nlm.nih.gov/40418571/") == "PMID:40418571"
    assert verify.paper_id("arXiv 2106.09685v2") == "2106.09685"
    assert verify.paper_id("huggingface.co/datasets/imdb") is None
    assert verify.paper_id("https://aclanthology.org/2024.acl-long.1/") == "2024.acl-long.1"


def test_keyed_sources_are_asked_only_with_their_keys(tools, monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert tools.default_sources() == ("openalex",)
    asked = []
    monkeypatch.setitem(tools.SOURCES, "openalex", lambda q, n, since=None: asked.append("oa") or [])
    monkeypatch.setattr(tools, "s2", lambda *a, **k: asked.append("s2") or [])
    rows = tools.search("x", 3, None, ("openalex", "s2", "scholar"))
    assert asked == ["oa"], "no request without a key"
    assert rows == [{"note": "not asked -- s2 (no SEMANTIC_SCHOLAR_API_KEY), scholar (no SERPAPI_API_KEY)"}]
    with pytest.raises(SystemExit, match="SEMANTIC_SCHOLAR_API_KEY"):
        tools._s2_headers()
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    assert tools.default_sources() == ("openalex", "s2"), "Scholar costs a credit: by name only"


def test_the_runs_choice_is_obeyed(tools, monkeypatch, tmp_path):
    from research_agent.bootstrap import config

    monkeypatch.delenv("RESEARCH_SOURCES", raising=False)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    config.write_sources(tmp_path, ["pubmed", "europepmc"])
    (tmp_path / "research").mkdir()
    monkeypatch.chdir(tmp_path / "research")          # the researcher works below the run folder
    assert tools.chosen() == ["pubmed", "europepmc"]
    assert tools.default_sources() == ("pubmed", "europepmc")
    asked = []
    for name in ("openalex", "pubmed", "europepmc"):
        monkeypatch.setitem(tools.SOURCES, name, lambda q, n, since=None, name=name: asked.append(name) or [])
    rows = tools.search("x", 3, None, ("openalex", "pubmed"))
    assert asked == ["pubmed"], "a source turned off is never asked, even by name"
    assert rows == [{"note": "not asked -- openalex (turned off for this run)"}]
    assert tools.main(["openalex", "x"]) == 1
    monkeypatch.setenv("RESEARCH_SOURCES", "openalex")
    assert tools.default_sources() == ("openalex",), "the variable wins"


def test_ml_power_numbers():
    spec = importlib.util.spec_from_file_location(
        "ml_power", TOOL.parents[1] / "ml-statistics" / "ml_power.py")
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    # 500 items at 80%, independent errors: ~7 points is the smallest gap seen.
    assert ml.accuracy(500, 0.8, None, None, 0.05, 0.8)["detectable_gap_points"] == pytest.approx(7.09, abs=0.05)
    assert ml.accuracy(None, 0.8, 0.02, 0.10, 0.05, 0.8)["test_items_needed"] == 1963
    assert ml.seeds(0.8, 1.0, None, 0.05, 0.8)["runs_per_system"] == 12
    w = ml.winrate(130, 200, None, 0.5, 0.05)
    assert w["beats_coin_flip"] and w["interval"][0] > 0.5
    assert [r["significant"] for r in ml.holm([0.01, 0.04, 0.03], 0.05)] == [True, False, False]
    same = ml.bootstrap([1, 0, 1, 1], [1, 0, 1, 1], 200, 0.05, 0)
    assert same["mean_difference"] == 0


@pytest.fixture(scope="module")
def venues(tools):
    import venues as v                    # research_tools put its folder on the path
    return v


def test_bibtex_of_both_styles(venues):
    text = '''@inproceedings{x-2024-a,
    title = "Few-Shot {I}n-Context Selection",
    year = "2024",
    url = "https://aclanthology.org/2024.acl-long.7/",
    abstract = "We select {demonstrations} by search; ``quotes'' and a {\\'e}t{\\'e}.",
}
@InProceedings{pmlr-v235-abe24a,
  title = 	 {{I}nfer{C}ept: Efficient <em>Intercept</em>},
  year = 	 {2024},
  abstract = 	 {Nested {braces {deep}} stay.}
}'''
    a, b = list(venues.parse_bibtex(text))
    assert a["title"] == "Few-Shot In-Context Selection" and a["year"] == "2024"
    assert a["abstract"].startswith("We select demonstrations by search")
    assert b["title"] == "InferCept: Efficient Intercept" and b["abstract"] == "Nested braces deep stay."


def test_acl_ids_say_their_venue(venues):
    assert venues.acl_id_venue("2024.acl-long.12") == ("acl", "long")
    assert venues.acl_id_venue("2023.findings-emnlp.45") == ("emnlp", "findings")
    assert venues.acl_id_venue("2024.tacl-1.5") == ("tacl", "1")
    assert venues.acl_id_venue("P19-1001") == ("acl", "")
    assert venues.acl_id_venue("D19-1001") == ("emnlp", "")


def test_a_topic_must_mostly_match(venues):
    q = venues.terms("few-shot demonstration selection")
    assert venues.score(q, "Demonstration Selection for Few-Shot Learning", "") > 0
    assert venues.score(q, "Machine Translation", "We translate.") == 0


def test_where_a_pdf_is(venues):
    get = lambda url: b""
    assert venues.pdf_url("2024.acl-long.1", get) == "https://aclanthology.org/2024.acl-long.1.pdf"
    assert venues.pdf_url("https://aclanthology.org/P19-1001/", get) == "https://aclanthology.org/P19-1001.pdf"
    assert venues.pdf_url("arXiv:2106.09685", get) == "https://arxiv.org/pdf/2106.09685"
    assert venues.pdf_url("https://proceedings.mlr.press/v235/abe24a.html", get) == \
        "https://raw.githubusercontent.com/mlresearch/v235/main/assets/abe24a/abe24a.pdf"
    assert venues.pdf_url("openreview:abc123", get) == "https://openreview.net/pdf?id=abc123"


def test_openreview_keeps_accepted_main_conference_papers(venues, monkeypatch, tmp_path):
    import json as j
    monkeypatch.setenv("RESEARCH_TOOLS_CACHE", str(tmp_path))
    notes = [{"id": i, "content": {k: {"value": v} for k, v in c.items()}} for i, c in [
        ("a", {"title": "Example Selection for ICL", "abstract": "selection of examples",
               "venue": "ICLR 2025 Poster", "venueid": "ICLR.cc/2025/Conference",
               "primary_area": "foundation or frontier models, including LLMs"}),
        ("b", {"title": "Example Selection Rejected", "abstract": "selection",
               "venue": "Submitted to ICLR 2025", "venueid": "ICLR.cc/2025/Conference/Rejected_Submission"}),
        ("c", {"title": "Example Selection at a workshop", "abstract": "selection",
               "venue": "ME-FoMo @ ICLR 2024 Poster", "venueid": "ICLR.cc/2024/Workshop/ME-FoMo"}),
        ("d", {"title": "Example Selection in Vision", "abstract": "selection",
               "venue": "ICLR 2024 Poster", "venueid": "ICLR.cc/2024/Conference",
               "primary_area": "applications_to_computer_vision"})]]
    get = lambda url: j.dumps({"notes": notes}).encode()
    rows = venues.search("example selection", ["iclr"], get=get)
    assert [r["id"] for r in rows] == ["openreview:a", "openreview:d"]
    rows = venues.search("example selection", ["iclr"], track="computer vision", get=get)
    assert [r["id"] for r in rows] == ["openreview:d"], "the track is the primary area"
