from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import (approve_taxonomy, compare_taxonomy_runs, derive_taxonomy_alternative,
                       discover, load_json, propose_taxonomy, score_taxonomy, require_approval,
                       score_skill_phrases, validate_manifest, validate_phrase_artifacts,
                       validate_score_artifacts, validate_study)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hotel-skill-study")
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--study", type=Path, default=Path("configs/hotel_skill_study.json"))
    v.add_argument("--models", type=Path, default=Path("configs/models.json"))
    d = sub.add_parser("discover")
    d.add_argument("--study", type=Path, default=Path("configs/hotel_skill_study.json"))
    d.add_argument("--models", type=Path, default=Path("configs/models.json"))
    d.add_argument("--run-dir", type=Path, required=True)
    d.add_argument("--backend", choices=("fixture", "transformers"), default="fixture")
    d.add_argument("--fixtures", type=Path, default=Path("fixtures/discovery_outputs.jsonl"))
    t = sub.add_parser("taxonomy")
    t.add_argument("--run-dir", type=Path, required=True)
    t.add_argument("--synonyms", type=Path, default=Path("configs/synonyms.json"))
    t.add_argument("--output", type=Path, required=True)
    a = sub.add_parser("approve")
    a.add_argument("--taxonomy", type=Path, required=True)
    a.add_argument("--approval", type=Path, required=True)
    a.add_argument("--reviewer", required=True); a.add_argument("--note", required=True)
    a.add_argument("--scope", choices=("human", "fixture_test"), default="human")
    a.add_argument("--review-command", help="Verbatim human approval decision retained as provenance")
    ta = sub.add_parser("taxonomy-alternative")
    ta.add_argument("--taxonomy", type=Path, required=True)
    ta.add_argument("--output", type=Path, required=True)
    ta.add_argument("--alternative-id", default="reviewed_splits_v1")
    s = sub.add_parser("score")
    s.add_argument("--taxonomy", type=Path, required=True)
    s.add_argument("--approval", type=Path, required=True)
    s.add_argument("--study", type=Path, default=Path("configs/hotel_skill_study.json"))
    s.add_argument("--models", type=Path, default=Path("configs/models.json"))
    s.add_argument("--output-dir", type=Path, required=True)
    s.add_argument("--backend", choices=("fixture", "transformers"), default="transformers")
    ps = sub.add_parser("score-phrases")
    ps.add_argument("--taxonomy", type=Path, required=True)
    ps.add_argument("--approval", type=Path, required=True)
    ps.add_argument("--study", type=Path, default=Path("configs/hotel_skill_study.json"))
    ps.add_argument("--models", type=Path, default=Path("configs/qwen_models.json"))
    ps.add_argument("--config", type=Path, default=Path("configs/qwen_phrase_scoring.json"))
    ps.add_argument("--output-dir", type=Path, required=True)
    pv = sub.add_parser("validate-phrase-artifacts")
    pv.add_argument("--taxonomy", type=Path, required=True)
    pv.add_argument("--approval", type=Path, required=True)
    pv.add_argument("--study", type=Path, default=Path("configs/hotel_skill_study.json"))
    pv.add_argument("--models", type=Path, default=Path("configs/qwen_models.json"))
    pv.add_argument("--config", type=Path, default=Path("configs/qwen_phrase_scoring.json"))
    pv.add_argument("--output-dir", type=Path, required=True)
    av = sub.add_parser("validate-artifacts")
    av.add_argument("--taxonomy", type=Path, required=True)
    av.add_argument("--approval", type=Path, required=True)
    av.add_argument("--study", type=Path, default=Path("configs/hotel_skill_study.json"))
    av.add_argument("--models", type=Path, default=Path("configs/models.json"))
    av.add_argument("--output-dir", type=Path, required=True)
    tc = sub.add_parser("compare-taxonomies")
    tc.add_argument("--base-taxonomy", type=Path, required=True)
    tc.add_argument("--base-approval", type=Path, required=True)
    tc.add_argument("--base-run-dir", type=Path, required=True)
    tc.add_argument("--alternative-taxonomy", type=Path, required=True)
    tc.add_argument("--alternative-approval", type=Path, required=True)
    tc.add_argument("--alternative-run-dir", type=Path, required=True)
    tc.add_argument("--output-dir", type=Path, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "validate":
        problems = validate_study(load_json(args.study)) + validate_manifest(load_json(args.models))
        if problems:
            print("\n".join(f"ERROR {x}" for x in problems)); return 1
        print("Configuration valid"); return 0
    if args.command == "discover":
        print(json.dumps(discover(args.study, args.models, args.run_dir, backend=args.backend,
                                  fixtures_path=args.fixtures if args.backend == "fixture" else None), indent=2)); return 0
    if args.command == "taxonomy":
        doc = propose_taxonomy(args.run_dir, args.synonyms, args.output)
        print(json.dumps({"status": doc["status"], "categories": len(doc["categories"]),
                          "mappings": len(doc["mappings"]), "taxonomy_sha256": doc["taxonomy_sha256"]}, indent=2)); return 0
    if args.command == "approve":
        print(json.dumps(approve_taxonomy(args.taxonomy, args.approval, args.reviewer, args.note,
                                         args.scope, args.review_command), indent=2)); return 0
    if args.command == "taxonomy-alternative":
        doc = derive_taxonomy_alternative(args.taxonomy, args.output, args.alternative_id)
        print(json.dumps({"status": doc["status"], "taxonomy_version": doc["taxonomy_version"],
                          "categories": len(doc["categories"]),
                          "taxonomy_sha256": doc["taxonomy_sha256"]}, indent=2)); return 0
    if args.command == "score":
        try:
            require_approval(args.taxonomy, args.approval)
            print(json.dumps(score_taxonomy(args.study, args.models, args.taxonomy, args.approval,
                                            args.output_dir, backend=args.backend), indent=2))
            return 0
        except PermissionError as exc:
            print(str(exc)); return 2
    if args.command == "score-phrases":
        try:
            print(json.dumps(score_skill_phrases(args.study, args.models, args.taxonomy,
                                                args.approval, args.config, args.output_dir), indent=2))
            return 0
        except PermissionError as exc:
            print(str(exc)); return 2
    if args.command == "validate-phrase-artifacts":
        problems = validate_phrase_artifacts(args.study, args.models, args.taxonomy,
                                             args.approval, args.config, args.output_dir)
        if problems:
            print("\n".join(f"ERROR {x}" for x in problems)); return 1
        print("Phrase-scoring artifacts valid"); return 0
    if args.command == "validate-artifacts":
        problems = validate_score_artifacts(args.study, args.models, args.taxonomy,
                                            args.approval, args.output_dir)
        if problems:
            print("\n".join(f"ERROR {x}" for x in problems)); return 1
        print("Scoring artifacts valid"); return 0
    if args.command == "compare-taxonomies":
        print(json.dumps(compare_taxonomy_runs(args.base_taxonomy, args.base_approval,
                                               args.base_run_dir, args.alternative_taxonomy,
                                               args.alternative_approval, args.alternative_run_dir,
                                               args.output_dir), indent=2)); return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
