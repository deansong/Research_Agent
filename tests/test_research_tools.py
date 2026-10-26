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
    assert tools.default_sources() == ("openalex", "s2", "scholar")
