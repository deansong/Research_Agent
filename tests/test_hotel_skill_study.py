"""Run with: python tests/test_hotel_skill_study.py"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hotel_skill_study.pipeline import (bootstrap_interval, choice_probability, discover,
                                        load_json, propose_taxonomy, require_approval,
                                        score_taxonomy, validate_manifest,
                                        validate_score_artifacts, validate_study)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_configs_validate():
    assert validate_study(load_json(ROOT / "configs/hotel_skill_study.json")) == []
    assert validate_manifest(load_json(ROOT / "configs/models.json")) == []


def test_fixture_discovery_is_complete_and_resumable():
    with tempfile.TemporaryDirectory() as tmp:
        run = pathlib.Path(tmp) / "run"
        first = discover(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json", run,
                         backend="fixture", fixtures_path=ROOT / "fixtures/discovery_outputs.jsonl")
        assert first == {"success": 18}
        records = [json.loads(p.read_text()) for p in (run / "records").glob("*.json")]
        assert len(records) == 18 and all(r["runtime"]["fixture"] for r in records)
        second = discover(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json", run,
                          backend="fixture", fixtures_path=ROOT / "fixtures/discovery_outputs.jsonl")
        assert second == {"cached": 18}


def test_taxonomy_maps_every_extracted_phrase_and_preserves_emergence():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp); run = root / "run"; output = root / "taxonomy.json"
        discover(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json", run,
                 backend="fixture", fixtures_path=ROOT / "fixtures/discovery_outputs.jsonl")
        taxonomy = propose_taxonomy(run, ROOT / "configs/synonyms.json", output)
        assert taxonomy["status"] == "proposed_unapproved" and taxonomy["fixture_data"] is True
        assert taxonomy["coverage"]["mapping_coverage"] == 1.0
        assert sum(c["occurrence_count"] for c in taxonomy["categories"]) == len(taxonomy["mappings"])
        ai = next(c for c in taxonomy["categories"] if c["label"] == "AI literacy")
        assert "potentially_emergent_review" in ai["review_flags"]
        try:
            require_approval(output, root / "missing-approval.json")
        except PermissionError:
            pass
        else:
            raise AssertionError("scoring gate accepted a missing approval")


def test_scoring_math_is_stable_and_seeded():
    assert abs(choice_probability(-1.0, -2.0) - 0.7310585) < 1e-6
    one = bootstrap_interval([0.2, 0.5, 0.8], iterations=100, level=0.95, seed=7)
    two = bootstrap_interval([0.2, 0.5, 0.8], iterations=100, level=0.95, seed=7)
    assert one == two and one[0] <= 0.5 <= one[1]


def test_approved_fixture_scoring_is_complete_traceable_and_resumable():
    taxonomy_path = ROOT / "artifacts/taxonomy/proposal.json"
    approval_path = ROOT / "artifacts/taxonomy/approval.json"
    taxonomy, approval = require_approval(taxonomy_path, approval_path)
    assert approval["taxonomy_version"] == "v1"
    assert approval["taxonomy_sha256"] == "d5270d9bd619f1316fcfb599145c51919f1d0e6110f1a6eb8cddb8c20eeecdab"
    with tempfile.TemporaryDirectory() as tmp:
        output = pathlib.Path(tmp) / "scores"
        first = score_taxonomy(ROOT / "configs/hotel_skill_study.json",
                               ROOT / "configs/models.json", taxonomy_path,
                               approval_path, output, backend="fixture")
        assert first == {"success": 2 * len(taxonomy["categories"]) * 3}
        assert validate_score_artifacts(ROOT / "configs/hotel_skill_study.json",
                                        ROOT / "configs/models.json", taxonomy_path,
                                        approval_path, output) == []
        records = [json.loads(p.read_text()) for p in (output / "records").glob("*.json")]
        assert all(r["fixture_data"] and r["runtime"]["synthetic_fixture"] for r in records)
        second = score_taxonomy(ROOT / "configs/hotel_skill_study.json",
                                ROOT / "configs/models.json", taxonomy_path,
                                approval_path, output, backend="fixture")
        assert second == {"cached": len(records)}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
    print("All hotel skill study tests passed.")
