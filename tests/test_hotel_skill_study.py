"""Run with: python tests/test_hotel_skill_study.py"""
from __future__ import annotations

import json
import copy
import pathlib
import sys
import tempfile
from types import SimpleNamespace

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hotel_skill_study.pipeline import (approve_taxonomy, bootstrap_interval, choice_probability,
                                        _chat_input_ids, _choice_logprob,
                                        compare_taxonomy_runs, derive_taxonomy_alternative,
                                        discover, load_json, propose_taxonomy, require_approval,
                                        score_taxonomy, validate_manifest,
                                        validate_score_artifacts, validate_study)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_chat_input_adapter_handles_transformers_five_batch_encoding():
    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            assert messages == [{"role": "user", "content": "prompt"}]
            assert kwargs["enable_thinking"] is False
            return {"input_ids": "tensor", "attention_mask": "mask"}
    assert _chat_input_ids(Tokenizer(), "prompt", {
        "chat_template_kwargs": {"enable_thinking": False}}) == "tensor"


def test_target_logit_and_logsumexp_reproduce_token_probability():
    class Tokenizer:
        def __call__(self, value, **kwargs):
            return {"input_ids": torch.tensor([[1]])}
        def decode(self, token_ids):
            return " Yes"
    class Model:
        device = torch.device("cpu")
        def __call__(self, input_ids):
            logits = torch.zeros((1, input_ids.shape[1], 3), dtype=torch.float32)
            logits[0, 1] = torch.tensor([-1.0, 2.0, 0.5])
            return SimpleNamespace(logits=logits)
    total, tokens = _choice_logprob(Model(), torch, torch.tensor([[0, 2]]), Tokenizer(), " Yes")
    assert len(tokens) == 1 and total == tokens[0]["log_probability"]
    assert abs(tokens[0]["target_logit"] - tokens[0]["logsumexp_all_vocabulary_logits"] -
               tokens[0]["log_probability"]) < 1e-7


def test_configs_validate():
    assert validate_study(load_json(ROOT / "configs/hotel_skill_study.json")) == []
    assert validate_manifest(load_json(ROOT / "configs/models.json")) == []


def test_qwen_release_generation_manifest_is_pinned_and_explicit():
    manifest = load_json(ROOT / "configs/qwen_models.json")
    assert validate_manifest(manifest) == []
    assert manifest["comparison_axis"] == "model_release_generation"
    selected = [model for model in manifest["models"] if model["selected"]]
    assert [model["checkpoint"] for model in selected] == [
        "Qwen/Qwen1.5-0.5B-Chat", "Qwen/Qwen3.5-0.8B"]
    assert all(len(model["revision"]) == 40 and
               model["revision"] == model["tokenizer_revision"] for model in selected)
    assert all(model["cutoff"]["status"] == "unknown" for model in selected)
    assert selected[1]["auto_model_class"] == "AutoModelForImageTextToText"
    assert selected[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_qwen_fixture_discovery_and_taxonomy_stop_unapproved():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp); run = root / "run"; proposal = root / "proposal.json"
        counts = discover(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/qwen_models.json",
                          run, backend="fixture", fixtures_path=ROOT / "fixtures/qwen_discovery_outputs.jsonl")
        assert counts == {"success": 18}
        taxonomy = propose_taxonomy(run, ROOT / "configs/synonyms.json", proposal)
        assert taxonomy["status"] == "proposed_unapproved" and taxonomy["fixture_data"] is True
        assert {mapping["model_id"] for mapping in taxonomy["mappings"]} == {
            "qwen1_5_0_5b_chat_2024", "qwen3_5_0_8b_2026"}
        try:
            require_approval(proposal, root / "approval.json")
        except PermissionError as exc:
            assert "no explicit human approval" in str(exc)
        else:
            raise AssertionError("unapproved Qwen taxonomy passed the scoring gate")


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


def test_discovery_rejects_cross_backend_and_changed_inputs_in_same_run():
    study = load_json(ROOT / "configs/hotel_skill_study.json")
    models = load_json(ROOT / "configs/models.json")
    changes = []
    changed = copy.deepcopy(study); changed["role"]["display_name"] = "concierge"; changes.append((changed, models, "fixture"))
    changed = copy.deepcopy(study); changed["prompt_variants"][0]["template"] += " Be specific."; changes.append((changed, models, "fixture"))
    changed_models = copy.deepcopy(models); changed_models["models"][0]["tokenizer_revision"] = "different"; changes.append((study, changed_models, "fixture"))
    changes.append((study, models, "transformers"))
    for changed_study, changed_models, backend in changes:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); run = root / "run"
            study_path = root / "study.json"; models_path = root / "models.json"
            study_path.write_text(json.dumps(study)); models_path.write_text(json.dumps(models))
            discover(study_path, models_path, run, backend="fixture",
                     fixtures_path=ROOT / "fixtures/discovery_outputs.jsonl")
            study_path.write_text(json.dumps(changed_study)); models_path.write_text(json.dumps(changed_models))
            try:
                discover(study_path, models_path, run, backend=backend,
                         fixtures_path=ROOT / "fixtures/discovery_outputs.jsonl" if backend == "fixture" else None)
            except ValueError as exc:
                assert "incompatible existing run manifest" in str(exc)
            else:
                raise AssertionError("discovery reused an incompatible run")


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


def test_approval_gate_detects_taxonomy_content_tampering():
    with tempfile.TemporaryDirectory() as tmp:
        changed_path = pathlib.Path(tmp) / "changed.json"
        changed = load_json(ROOT / "artifacts/taxonomy/proposal.json")
        changed["categories"][0]["label"] = "Silently changed"
        changed_path.write_text(json.dumps(changed))
        try:
            require_approval(changed_path, ROOT / "artifacts/taxonomy/approval.json")
        except PermissionError as exc:
            assert "embedded digest" in str(exc)
        else:
            raise AssertionError("approval gate accepted modified taxonomy content")


def test_fixture_test_approval_is_labelled_and_cannot_authorize_empirical_scoring():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp); approval_path = root / "approval.json"
        approval = approve_taxonomy(ROOT / "artifacts/qwen_taxonomy/proposal.json", approval_path,
                                    "automated fixture test harness",
                                    "Synthetic scoring validation only; not human taxonomy approval.",
                                    "fixture_test")
        assert approval["taxonomy_sha256"] == "3095201c9ad6c3243f2a60a63dc4959e4d3252a54a4f8b7af3f6305a55ca03cc"
        assert approval["approval_scope"] == "fixture_test"
        assert approval["human_command"] is None
        assert approval["human_notes"] is None
        assert approval["approval_source"] == "automated_fixture_test_authorization"
        require_approval(ROOT / "artifacts/qwen_taxonomy/proposal.json", approval_path)
        try:
            score_taxonomy(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/qwen_models.json",
                           ROOT / "artifacts/qwen_taxonomy/proposal.json", approval_path,
                           root / "empirical", backend="transformers")
        except PermissionError as exc:
            assert "fixture-test approval cannot authorize real checkpoint scoring" in str(exc)
        else:
            raise AssertionError("fixture-test approval authorized empirical scoring")


def test_scoring_rejects_changed_role_prompt_and_tokenizer_in_same_run():
    taxonomy = ROOT / "artifacts/taxonomy/proposal.json"; approval = ROOT / "artifacts/taxonomy/approval.json"
    study = load_json(ROOT / "configs/hotel_skill_study.json"); models = load_json(ROOT / "configs/models.json")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp); output = root / "scores"
        sp = root / "study.json"; mp = root / "models.json"
        sp.write_text(json.dumps(study)); mp.write_text(json.dumps(models))
        score_taxonomy(sp, mp, taxonomy, approval, output, backend="fixture")
        for kind in ("role", "prompt", "tokenizer"):
            changed_study, changed_models = copy.deepcopy(study), copy.deepcopy(models)
            if kind == "role": changed_study["role"]["display_name"] = "concierge"
            if kind == "prompt": changed_study["scoring"]["prompt_variants"][0]["template"] += " Be exact."
            if kind == "tokenizer": changed_models["models"][0]["tokenizer_revision"] = "different"
            sp.write_text(json.dumps(changed_study)); mp.write_text(json.dumps(changed_models))
            try:
                score_taxonomy(sp, mp, taxonomy, approval, output, backend="fixture")
            except ValueError as exc:
                assert "incompatible existing run manifest" in str(exc)
            else:
                raise AssertionError(f"scoring reused incompatible {kind} cache")
        try:
            score_taxonomy(sp, mp, taxonomy, approval, output, backend="transformers")
        except ValueError as exc:
            assert "incompatible existing run manifest" in str(exc)
        else:
            raise AssertionError("scoring reused a cross-backend cache")


def test_validator_detects_record_summary_missing_and_manifest_tampering():
    taxonomy = ROOT / "artifacts/taxonomy/proposal.json"; approval = ROOT / "artifacts/taxonomy/approval.json"
    with tempfile.TemporaryDirectory() as tmp:
        output = pathlib.Path(tmp) / "scores"
        score_taxonomy(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json",
                       taxonomy, approval, output, backend="fixture")
        record_path = next((output / "records").glob("*.json")); original = record_path.read_text()
        record = json.loads(original); record["components"][0]["tokens"][0]["log_probability"] += 0.2
        record_path.write_text(json.dumps(record))
        assert any("component log-probability sum mismatch" in e for e in validate_score_artifacts(
            ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json", taxonomy, approval, output))
        record_path.write_text(original)
        summary_path = output / "tables/skill_scores.json"; original_summary = summary_path.read_text()
        summary = json.loads(original_summary); summary[0]["mean_model_relative_probability"] = 0.0
        summary_path.write_text(json.dumps(summary))
        assert any("mean_model_relative_probability mismatch" in e for e in validate_score_artifacts(
            ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json", taxonomy, approval, output))
        summary_path.write_text(original_summary)
        prompt_path = output / "tables/prompt_sensitivity.csv"
        prompt_path.write_text(prompt_path.read_text() + "tampered\n")
        assert any("provenance hash mismatch" in e for e in validate_score_artifacts(
            ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json", taxonomy, approval, output))
        missing_path = output / "tables/missing_cells.json"; missing = json.loads(missing_path.read_text())
        missing["missing_count"] = 1; missing_path.write_text(json.dumps(missing))
        assert any("missing-cell artifact" in e for e in validate_score_artifacts(
            ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json", taxonomy, approval, output))


def test_fixture_taxonomy_sensitivity_branch_is_versioned_gated_and_compared():
    base_taxonomy = ROOT / "artifacts/taxonomy/proposal.json"; base_approval = ROOT / "artifacts/taxonomy/approval.json"
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp); alternative = root / "alternative.json"; alt_approval = root / "approval.json"
        doc = derive_taxonomy_alternative(base_taxonomy, alternative)
        assert doc["alternative_of_sha256"] == load_json(base_taxonomy)["taxonomy_sha256"]
        assert len(doc["categories"]) > len(load_json(base_taxonomy)["categories"])
        try:
            score_taxonomy(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json",
                           alternative, alt_approval, root / "blocked", backend="fixture")
        except PermissionError:
            pass
        else:
            raise AssertionError("alternative scoring bypassed approval gate")
        approve_taxonomy(alternative, alt_approval, "test harness", "fixture-only branch test", "fixture_test")
        base_run, alt_run = root / "base", root / "alt"
        score_taxonomy(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json",
                       base_taxonomy, base_approval, base_run, backend="fixture")
        score_taxonomy(ROOT / "configs/hotel_skill_study.json", ROOT / "configs/models.json",
                       alternative, alt_approval, alt_run, backend="fixture")
        result = compare_taxonomy_runs(base_taxonomy, base_approval, base_run,
                                       alternative, alt_approval, alt_run, root / "comparison")
        assert result["comparison_rows"] > 28
        comparison = load_json(root / "comparison/comparison_manifest.json")
        assert comparison["fixture_data"] and comparison["compared_quantities"] == [
            "coverage", "ranks", "rank directions", "intervals", "prompt ranges"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
    print("All hotel skill study tests passed.")
