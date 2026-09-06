from __future__ import annotations

import csv
import copy
import hashlib
import json
import math
import os
import platform
import random
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
VALID_CUTOFF = {"documented_cutoff", "documented_period", "inferred", "unknown"}
VALID_COMPARISON_AXIS = {"training_period", "model_release_generation"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_revision() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_manifest(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    selected_dates: list[str] = []
    comparison_axis = doc.get("comparison_axis", "training_period")
    if comparison_axis not in VALID_COMPARISON_AXIS:
        errors.append(f"comparison_axis: must be one of {sorted(VALID_COMPARISON_AXIS)}")
    for i, model in enumerate(doc.get("models", [])):
        where = f"models[{i}]"
        required = ("id", "checkpoint", "release_date", "release_source", "cutoff", "selected")
        for field in required:
            if field not in model:
                errors.append(f"{where}.{field}: missing")
        mid = model.get("id")
        if mid in ids:
            errors.append(f"{where}.id: duplicate {mid!r}")
        ids.add(mid)
        cutoff = model.get("cutoff", {})
        status = cutoff.get("status")
        if status not in VALID_CUTOFF:
            errors.append(f"{where}.cutoff.status: must be one of {sorted(VALID_CUTOFF)}")
        if status.startswith("documented"):
            if not cutoff.get("value") or not cutoff.get("sources") or not cutoff.get("latest_date"):
                errors.append(f"{where}.cutoff: documented evidence needs value, latest_date, and sources")
        if cutoff.get("latest_date") == model.get("release_date") and status.startswith("documented"):
            errors.append(f"{where}: cutoff equals release date; verify this is evidence, not substitution")
        if model.get("selected"):
            if comparison_axis == "training_period" and status not in {"documented_cutoff", "documented_period"}:
                errors.append(f"{where}: selected models require documented cutoff/period")
            if not model.get("revision") or not model.get("tokenizer_revision"):
                errors.append(f"{where}: selected checkpoints and tokenizers must be revision-pinned")
            if not model.get("auto_model_class"):
                errors.append(f"{where}: selected models require an explicit auto_model_class adapter")
            selected_dates.append(cutoff.get("latest_date", "") if comparison_axis == "training_period"
                                  else model.get("release_date", ""))
    if len(selected_dates) < 2 or len(set(selected_dates)) < 2:
        qualifier = "documented periods" if comparison_axis == "training_period" else "release dates"
        errors.append(f"at least two selected models with distinct {qualifier} are required")
    return errors


def _auto_model_class(transformers: Any, model_cfg: dict[str, Any]) -> Any:
    name = model_cfg.get("auto_model_class")
    allowed = {
        "AutoModelForCausalLM": "AutoModelForCausalLM",
        "AutoModelForImageTextToText": "AutoModelForImageTextToText",
    }
    if name not in allowed:
        raise ValueError(f"unsupported auto_model_class {name!r}")
    return getattr(transformers, allowed[name])


def _chat_input_ids(tokenizer: Any, prompt: str, model_cfg: dict[str, Any]) -> Any:
    encoded = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}], add_generation_prompt=True,
        return_tensors="pt", **model_cfg.get("chat_template_kwargs", {}))
    # Transformers 5 returns BatchEncoding here while older supported versions
    # returned a tensor. The scoring code deliberately needs only input_ids.
    return encoded["input_ids"] if hasattr(encoded, "keys") else encoded


def validate_study(doc: dict[str, Any]) -> list[str]:
    errors = []
    role = doc.get("role", {})
    if not role.get("display_name") or not role.get("industry"):
        errors.append("role.display_name and role.industry are required")
    variants = doc.get("prompt_variants", [])
    if len(variants) < 2 or len({v.get("id") for v in variants}) != len(variants):
        errors.append("at least two uniquely named prompt variants are required")
    for variant in variants:
        template = variant.get("template", "")
        if "{role}" not in template or "{industry}" not in template:
            errors.append(f"prompt {variant.get('id')!r} must parameterize role and industry")
    if len(doc.get("seeds", [])) < 2:
        errors.append("repeated discovery requires at least two seeds")
    return errors


def render_prompt(study: dict[str, Any], variant: dict[str, Any]) -> str:
    role = study["role"]
    return variant["template"].format(role=role["display_name"], industry=role["industry"])


def _discovery_identity(model: dict[str, Any], variant: dict[str, Any], seed: int,
                        study: dict[str, Any], backend: str,
                        fixture_sha256: str | None) -> dict[str, Any]:
    return {"schema_version": 2, "backend": backend,
            "fixture_source_sha256": fixture_sha256,
            "checkpoint": model["checkpoint"], "revision": model["revision"],
            "tokenizer_checkpoint": model["tokenizer_checkpoint"],
            "tokenizer_revision": model["tokenizer_revision"],
            "architecture": model["architecture"], "prompt_adapter": model["prompt_adapter"],
            "auto_model_class": model["auto_model_class"],
            "chat_template_kwargs": model.get("chat_template_kwargs", {}),
            "dtype": model.get("dtype", "auto"),
            "role": study["role"], "prompt_variant": variant,
            "rendered_prompt": render_prompt(study, variant), "seed": seed,
            "generation": study["generation"]}


def _run_identity(study: dict[str, Any], manifest: dict[str, Any], backend: str,
                  fixture_sha256: str | None) -> dict[str, Any]:
    return {"schema_version": 2, "backend": backend,
            "study_config_sha256": digest(study), "model_manifest_sha256": digest(manifest),
            "fixture_source_sha256": fixture_sha256}


def _reject_incompatible_run(path: Path, expected_identity: dict[str, Any]) -> None:
    if not path.exists():
        records_dir = path.parent / "records"
        if records_dir.exists() and any(records_dir.glob("*.json")):
            raise ValueError(f"records exist without a compatible run manifest at {path}; use a new output directory")
        return
    existing = load_json(path)
    if existing.get("run_identity") != expected_identity:
        raise ValueError(f"incompatible existing run manifest at {path}; use a new output directory")


def _fixture_index(path: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    return {(r["model_id"], r["prompt_variant"], r["seed"]): r for r in json_lines(path)}


def _transformers_generate(model_cfg: dict[str, Any], prompt: str, seed: int,
                           generation: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        import torch
        import transformers
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("install optional inference dependencies: pip install torch transformers accelerate") from exc
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    tokenizer = AutoTokenizer.from_pretrained(model_cfg["tokenizer_checkpoint"],
                                              revision=model_cfg["tokenizer_revision"])
    model_class = _auto_model_class(transformers, model_cfg)
    model = model_class.from_pretrained(model_cfg["checkpoint"], revision=model_cfg["revision"],
                                        device_map="auto", dtype=model_cfg.get("dtype", "auto"))
    if model_cfg["prompt_adapter"] == "chat_template":
        encoded = _chat_input_ids(tokenizer, prompt, model_cfg)
    else:
        encoded = tokenizer(prompt, return_tensors="pt")["input_ids"]
    encoded = encoded.to(model.device)
    kwargs = dict(generation)
    if tokenizer.pad_token_id is None:
        kwargs["pad_token_id"] = tokenizer.eos_token_id
    output = model.generate(encoded, **kwargs)
    text = tokenizer.decode(output[0, encoded.shape[-1]:], skip_special_tokens=True)
    details = {"transformers_version": transformers.__version__, "torch_version": torch.__version__,
               "resolved_model_name_or_path": model.name_or_path,
               "resolved_tokenizer_name_or_path": tokenizer.name_or_path,
               "tokenizer_class": tokenizer.__class__.__name__,
               "model_class": model.__class__.__name__, "auto_model_class": model_cfg["auto_model_class"],
               "requested_dtype": model_cfg.get("dtype", "auto"), "device": str(model.device),
               "determinism_note": "seed set for CPU and CUDA; kernels may still be nondeterministic"}
    return text, details


def discover(study_path: Path, models_path: Path, run_dir: Path, *, backend: str,
             fixtures_path: Path | None = None) -> dict[str, int]:
    study, manifest = load_json(study_path), load_json(models_path)
    problems = validate_study(study) + validate_manifest(manifest)
    if problems:
        raise ValueError("invalid configuration:\n- " + "\n- ".join(problems))
    run_dir.mkdir(parents=True, exist_ok=True)
    records_dir = run_dir / "records"
    records_dir.mkdir(exist_ok=True)
    fixture_sha256 = digest(json_lines(fixtures_path)) if fixtures_path else None
    run_identity = _run_identity(study, manifest, backend, fixture_sha256)
    _reject_incompatible_run(run_dir / "run_manifest.json", run_identity)
    atomic_json(run_dir / "run_manifest.json", {"schema_version": 2, "status": "running",
                "run_identity": run_identity, "backend": backend,
                "fixture_data": backend == "fixture", "updated_at": utc_now()})
    fixtures = _fixture_index(fixtures_path) if fixtures_path else {}
    counts = Counter()
    for model in (m for m in manifest["models"] if m["selected"]):
        for variant in study["prompt_variants"]:
            for seed in study["seeds"]:
                identity = _discovery_identity(model, variant, seed, study, backend, fixture_sha256)
                key = digest(identity)
                target = records_dir / f"{key}.json"
                if target.exists():
                    cached = load_json(target)
                    if cached.get("cache_identity") != identity:
                        raise ValueError(f"cache identity mismatch in {target}")
                    counts["cached"] += 1; continue
                started = utc_now()
                record: dict[str, Any] = {
                    "schema_version": 2, "record_id": key, "cache_identity": identity,
                    "study_id": study["study_id"],
                    "role": study["role"], "model_id": model["id"],
                    "checkpoint": model["checkpoint"], "revision": model["revision"],
                    "tokenizer_checkpoint": model["tokenizer_checkpoint"],
                    "tokenizer_revision": model["tokenizer_revision"],
                    "architecture": model["architecture"], "prompt_adapter": model["prompt_adapter"],
                    "auto_model_class": model["auto_model_class"],
                    "chat_template_kwargs": model.get("chat_template_kwargs", {}),
                    "requested_dtype": model.get("dtype", "auto"),
                    "cutoff": model["cutoff"], "prompt_variant": variant["id"],
                    "prompt_template": variant["template"], "rendered_prompt": render_prompt(study, variant),
                    "seed": seed, "generation": study["generation"], "backend": backend,
                    "started_at": started
                }
                try:
                    if backend == "fixture":
                        fixture = fixtures[(model["id"], variant["id"], seed)]
                        raw, details = fixture["raw_text"], {
                            "fixture": True, "fixture_source": str(fixtures_path),
                            "warning": "Demonstration text supplied by the repository; not model inference."
                        }
                    elif backend == "transformers":
                        raw, details = _transformers_generate(model, record["rendered_prompt"], seed,
                                                              study["generation"])
                    else:
                        raise ValueError(f"unknown backend {backend!r}")
                    record.update(status="success", raw_output=raw, runtime=details)
                    counts["success"] += 1
                except Exception as exc:
                    record.update(status="failure", raw_output=None,
                                  failure={"type": type(exc).__name__, "message": str(exc),
                                           "retryable": isinstance(exc, (OSError, RuntimeError, KeyError))})
                    counts["failure"] += 1
                record["finished_at"] = utc_now()
                atomic_json(target, record)
    atomic_json(run_dir / "run_manifest.json", {
        "schema_version": 1, "study_config": str(study_path), "study_config_sha256": digest(study),
        "model_manifest": str(models_path), "model_manifest_sha256": digest(manifest),
        "backend": backend, "fixture_data": backend == "fixture", "updated_at": utc_now(),
        "record_counts": dict(counts), "run_identity": run_identity,
        "execution_status": "fixture_demo" if backend == "fixture" else "local_inference"
    })
    return dict(counts)


PREFIX = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
TRAILING = re.compile(r"\s*(?:[-–—:]\s+.*|[.;,:])$")


def clean_phrase(line: str) -> str | None:
    phrase = PREFIX.sub("", line).strip().strip('"“”')
    phrase = TRAILING.sub("", phrase).strip()
    if not phrase or len(phrase) > 120 or phrase.lower().startswith(("here are", "skills include")):
        return None
    return phrase


def normalize_phrase(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9+ ]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def propose_taxonomy(run_dir: Path, synonyms_path: Path, output_path: Path) -> dict[str, Any]:
    synonym_doc = load_json(synonyms_path)
    aliases: dict[str, tuple[str, float]] = {}
    for cluster in synonym_doc["clusters"]:
        for alias in cluster["aliases"]:
            aliases[normalize_phrase(alias)] = (cluster["canonical"], cluster["confidence"])
    phrases: list[dict[str, Any]] = []
    failures = []
    for path in sorted((run_dir / "records").glob("*.json")):
        record = load_json(path)
        if record["status"] != "success":
            failures.append({"record_id": record["record_id"], "failure": record.get("failure")})
            continue
        for position, line in enumerate(record["raw_output"].splitlines(), 1):
            raw = clean_phrase(line)
            if raw:
                norm = normalize_phrase(raw)
                if norm in aliases:
                    canonical, confidence = aliases[norm]
                    flags: list[str] = []
                else:
                    canonical = raw[0].upper() + raw[1:]
                    confidence = 0.55
                    flags = ["unclustered", "human_review_required"]
                phrases.append({"mapping_id": digest([record["record_id"], position, raw]),
                                "record_id": record["record_id"], "model_id": record["model_id"],
                                "prompt_variant": record["prompt_variant"], "seed": record["seed"],
                                "line_number": position, "raw_phrase": raw, "normalized_phrase": norm,
                                "proposed_canonical": canonical, "confidence": confidence,
                                "review_flags": flags, "fixture": bool(record.get("runtime", {}).get("fixture"))})
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for phrase in phrases:
        grouped[phrase["proposed_canonical"]].append(phrase)
    categories = []
    all_models = sorted({p["model_id"] for p in phrases})
    for name, evidence in sorted(grouped.items()):
        model_counts = Counter(p["model_id"] for p in evidence)
        flags = sorted({flag for p in evidence for flag in p["review_flags"]})
        if len(model_counts) == 1 and len(all_models) > 1:
            flags.append("model_specific_in_fixture")
        if name == "AI literacy":
            flags.append("potentially_emergent_review")
        categories.append({"canonical_id": re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_"),
                           "label": name, "occurrence_count": len(evidence),
                           "distinct_record_count": len({p["record_id"] for p in evidence}),
                           "model_counts": dict(sorted(model_counts.items())),
                           "raw_variants": dict(sorted(Counter(p["raw_phrase"] for p in evidence).items())),
                           "supporting_examples": [{k: p[k] for k in ("raw_phrase", "model_id", "record_id")}
                                                   for p in evidence[:5]],
                           "confidence": min(p["confidence"] for p in evidence),
                           "review_flags": sorted(set(flags)), "decision": "proposed"})
    body = {"schema_version": 1, "status": "proposed_unapproved", "fixture_data": all(p["fixture"] for p in phrases),
            "created_at": utc_now(), "method": {"normalization": "casefold, punctuation removal, whitespace collapse",
            "clustering": "exact normalized alias lookup from versioned conservative synonym file",
            "non_merge_policy": "unlisted phrases remain separate and are flagged; no embedding/fuzzy auto-merge"},
            "source_run": str(run_dir), "source_run_manifest_sha256": digest(load_json(run_dir / "run_manifest.json")),
            "synonym_config": str(synonyms_path), "synonym_config_sha256": digest(synonym_doc),
            "categories": categories, "mappings": phrases, "failures": failures,
            "coverage": {"successful_records": len({p["record_id"] for p in phrases}),
                         "raw_phrase_occurrences": len(phrases), "mapped_occurrences": len(phrases),
                         "mapping_coverage": 1.0 if phrases else 0.0}}
    body["taxonomy_sha256"] = digest(body)
    atomic_json(output_path, body)
    write_taxonomy_review_files(body, output_path.parent)
    return body


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _bar_svg(rows: list[tuple[str, float]], title: str, path: Path) -> None:
    if not rows:
        return
    width, left, row_h = 900, 310, 23
    scale = (width - left - 30) / max(value for _, value in rows)
    height = 55 + len(rows) * row_h
    def esc(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;")
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             '<style>text{font:12px sans-serif}.title{font:bold 15px sans-serif}.bar{fill:#2878b5}</style>',
             f'<text class="title" x="10" y="21">{esc(title)}</text>']
    for i, (label, value) in enumerate(rows):
        y = 42 + i * row_h
        parts.extend([f'<text x="10" y="{y+11}">{esc(label)}</text>',
                      f'<rect class="bar" x="{left}" y="{y}" width="{value*scale:.1f}" height="14"/>',
                      f'<text x="{left+value*scale+5:.1f}" y="{y+11}">{value:g}</text>'])
    parts.append('</svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_taxonomy_review_files(taxonomy: dict[str, Any], output_dir: Path) -> None:
    categories = [{"canonical_id": c["canonical_id"], "label": c["label"],
                   "occurrence_count": c["occurrence_count"],
                   "distinct_record_count": c["distinct_record_count"],
                   "confidence": c["confidence"],
                   "review_flags": "|".join(c["review_flags"]),
                   "model_counts_json": canonical_json(c["model_counts"])}
                  for c in taxonomy["categories"]]
    mappings = [{k: ("|".join(p[k]) if k == "review_flags" else p[k]) for k in
                 ("mapping_id", "record_id", "model_id", "prompt_variant", "seed", "line_number",
                  "raw_phrase", "normalized_phrase", "proposed_canonical", "confidence", "review_flags", "fixture")}
                for p in taxonomy["mappings"]]
    _write_csv(output_dir / "categories.csv", categories)
    _write_csv(output_dir / "raw_phrase_mappings.csv", mappings)
    bars = [(f'{c["label"]} · {model}', count) for c in taxonomy["categories"]
            for model, count in c["model_counts"].items()]
    _bar_svg(bars, "Fixture phrase prevalence by proposed category and model label",
             output_dir / "fixture_prevalence.svg")
    atomic_json(output_dir / "taxonomy_sensitivity_plan.json", {
        "taxonomy_sha256": taxonomy["taxonomy_sha256"],
        "alternatives_requiring_separate_review_and_approval": [
            {"id": "split_low_confidence_merges", "rule": "split configured synonym clusters with confidence below 0.85"},
            {"id": "split_pms_from_booking", "rule": "separate property-management-system phrases from generic reservations"},
            {"id": "split_conflict_from_complaints", "rule": "separate conflict resolution from complaint handling"}
        ],
        "comparison_outputs": ["category coverage", "within-model ranks", "rank direction",
                               "bootstrap intervals", "prompt range"],
        "status": "planned_not_scored_before_approval"
    })


def derive_taxonomy_alternative(taxonomy_path: Path, output_path: Path,
                                alternative_id: str = "reviewed_splits_v1") -> dict[str, Any]:
    """Create a versioned sensitivity proposal without mutating the approved taxonomy."""
    base = load_json(taxonomy_path)
    claimed = base.get("taxonomy_sha256")
    check = dict(base); check.pop("taxonomy_sha256", None)
    if digest(check) != claimed:
        raise ValueError("base taxonomy content does not match its digest")
    if alternative_id != "reviewed_splits_v1":
        raise ValueError(f"unsupported taxonomy alternative {alternative_id!r}")
    split_labels = {"Conflict resolution": "Conflict resolution", "Cash handling": "Cash handling",
                    "PMS proficiency": "PMS proficiency",
                    "Property management systems": "PMS proficiency",
                    "Accuracy": "Accuracy", "Prioritization": "Prioritization"}
    mappings = copy.deepcopy(base["mappings"])
    for mapping in mappings:
        if mapping["raw_phrase"] in split_labels:
            mapping["proposed_canonical"] = split_labels[mapping["raw_phrase"]]
            mapping["sensitivity_transformation"] = "split_from_approved_merge"
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mapping in mappings:
        grouped[mapping["proposed_canonical"]].append(mapping)
    categories = []
    for label, evidence in sorted(grouped.items()):
        model_counts = Counter(x["model_id"] for x in evidence)
        categories.append({"canonical_id": re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_"),
                           "label": label, "occurrence_count": len(evidence),
                           "distinct_record_count": len({x["record_id"] for x in evidence}),
                           "model_counts": dict(sorted(model_counts.items())),
                           "raw_variants": dict(sorted(Counter(x["raw_phrase"] for x in evidence).items())),
                           "supporting_examples": [{k: x[k] for k in ("raw_phrase", "model_id", "record_id")}
                                                   for x in evidence[:5]],
                           "confidence": min(x["confidence"] for x in evidence),
                           "review_flags": ["taxonomy_sensitivity_alternative"],
                           "decision": "proposed_sensitivity_alternative"})
    body = {"schema_version": 1, "status": "proposed_unapproved",
            "taxonomy_version": "v1-alt-reviewed-splits-v1",
            "fixture_data": base.get("fixture_data", False), "created_at": utc_now(),
            "alternative_id": alternative_id, "alternative_of_sha256": claimed,
            "method": {"transformation": "Split reviewer-flagged approved merges by exact raw phrase",
                       "split_labels": split_labels, "non_merge_policy": "All other v1 mappings unchanged"},
            "source_run": base.get("source_run"), "categories": categories, "mappings": mappings,
            "failures": copy.deepcopy(base.get("failures", [])),
            "coverage": {"successful_records": base["coverage"]["successful_records"],
                         "raw_phrase_occurrences": len(mappings), "mapped_occurrences": len(mappings),
                         "mapping_coverage": 1.0 if mappings else 0.0}}
    body["taxonomy_sha256"] = digest(body)
    atomic_json(output_path, body)
    return body


def approve_taxonomy(taxonomy_path: Path, approval_path: Path, reviewer: str, note: str,
                     approval_scope: str = "human", review_command: str | None = None) -> dict[str, Any]:
    taxonomy = load_json(taxonomy_path)
    if taxonomy.get("status") != "proposed_unapproved":
        raise ValueError("only a proposed_unapproved taxonomy can be approved")
    if approval_scope not in {"human", "fixture_test"}:
        raise ValueError("approval_scope must be human or fixture_test")
    if approval_scope == "fixture_test" and not taxonomy.get("fixture_data"):
        raise ValueError("fixture_test approval is allowed only for fixture-derived taxonomies")
    if approval_scope == "fixture_test" and review_command is not None:
        raise ValueError("fixture_test approval cannot record a human review command")
    approval = {"schema_version": 1, "decision": "approved", "taxonomy_sha256": taxonomy["taxonomy_sha256"],
                "taxonomy_version": taxonomy.get("taxonomy_version"),
                "approval_scope": approval_scope, "reviewer": reviewer, "note": note,
                "human_notes": note if approval_scope == "human" else None,
                "human_command": review_command if approval_scope == "human" else None,
                "fixture_test_note": note if approval_scope == "fixture_test" else None,
                "approval_source": ("automated_fixture_test_authorization" if approval_scope == "fixture_test"
                                    else "explicit_human_review_decision" if review_command
                                    else "human_cli_or_function_invocation"),
                "approved_at": utc_now()}
    atomic_json(approval_path, approval)
    return approval


def require_approval(taxonomy_path: Path, approval_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    taxonomy = load_json(taxonomy_path)
    claimed_digest = taxonomy.get("taxonomy_sha256")
    digest_input = dict(taxonomy)
    digest_input.pop("taxonomy_sha256", None)
    if not claimed_digest or digest(digest_input) != claimed_digest:
        raise PermissionError("scoring blocked: taxonomy content does not match its embedded digest")
    if not approval_path.exists():
        raise PermissionError("scoring blocked: taxonomy has no explicit human approval record")
    approval = load_json(approval_path)
    if approval.get("decision") != "approved" or approval.get("taxonomy_sha256") != taxonomy.get("taxonomy_sha256"):
        raise PermissionError("scoring blocked: approval does not match the exact taxonomy digest")
    return taxonomy, approval


def choice_probability(yes_logprob: float, no_logprob: float) -> float:
    high = max(yes_logprob, no_logprob)
    yes, no = math.exp(yes_logprob - high), math.exp(no_logprob - high)
    return yes / (yes + no)


def bootstrap_interval(values: list[float], *, iterations: int, level: float, seed: int) -> tuple[float, float]:
    if not values:
        return math.nan, math.nan
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(iterations))
    alpha = (1 - level) / 2
    return means[int(alpha * (iterations - 1))], means[int((1 - alpha) * (iterations - 1))]


def summarize_scores(rows: Iterable[dict[str, Any]], iterations: int = 2000,
                     level: float = 0.95) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("status") == "success":
            groups[(row["model_id"], row["canonical_id"])].append(row)
    out = []
    for (model_id, skill), items in sorted(groups.items()):
        vals = [float(x["calibrated_yes_probability"]) for x in items]
        lo, hi = bootstrap_interval(vals, iterations=iterations, level=level,
                                    seed=int(digest([model_id, skill])[:8], 16))
        fixture_data = all(bool(x.get("fixture_data")) for x in items)
        backend = items[0].get("backend")
        warning = ("SYNTHETIC FIXTURE: software verification only; not empirical."
                   if fixture_data else
                   "Model-relative elicitation; not a direct labour-market measurement.")
        out.append({"fixture_data": fixture_data, "backend": backend,
                    "artifact_warning": warning,
                    "model_id": model_id, "canonical_id": skill, "n": len(vals),
                    "mean_model_relative_probability": statistics.fmean(vals), "ci_low": lo, "ci_high": hi,
                    "prompt_range": max(vals) - min(vals),
                    "uncertainty_type": "descriptive_prompt_resampling_interval",
                    "uncertainty_warning": "Resamples the fixed prompt variants only; not a population confidence interval.",
                    "effective_prompt_sample_size": len(vals),
                    "measurement_record_ids_json": canonical_json([x["record_id"] for x in items]),
                    "prompt_means_json": canonical_json(dict(sorted(
                        (prompt, statistics.fmean(float(x["calibrated_yes_probability"])
                                                  for x in items if x["prompt_variant"] == prompt))
                        for prompt in {x["prompt_variant"] for x in items}))),
                    "metric_warning": "Within-model constrained-choice value; not a tokenizer-invariant raw probability."})
    return out


def _choice_logprob(model: Any, torch: Any, prompt_ids: Any, tokenizer: Any,
                    choice: str) -> tuple[float, list[dict[str, Any]]]:
    choice_ids = tokenizer(choice, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
    full = torch.cat([prompt_ids, choice_ids], dim=1)
    with torch.no_grad():
        logits = model(full).logits[0]
    start = prompt_ids.shape[1] - 1
    token_rows, total = [], 0.0
    for offset, token_id in enumerate(choice_ids[0].tolist()):
        position_logits = logits[start + offset].float()
        target_logit = float(position_logits[token_id].item())
        logsumexp = float(torch.logsumexp(position_logits, dim=-1).item())
        value = target_logit - logsumexp
        token_rows.append({"token_id": token_id, "token_text": tokenizer.decode([token_id]),
                           "target_logit": target_logit, "logsumexp_all_vocabulary_logits": logsumexp,
                           "normalization_identity": "log_probability = target_logit - logsumexp_all_vocabulary_logits",
                           "log_probability": value})
        total += value
    return total, token_rows


def _fixture_components(model_id: str, canonical_id: str, prompt_variant: str,
                        choices: list[str]) -> list[dict[str, Any]]:
    """Deterministic synthetic scores for pipeline verification, never evidence."""
    unit = int(digest(["synthetic-scoring-fixture-v1", model_id, canonical_id,
                       prompt_variant])[:12], 16) / float(16 ** 12 - 1)
    margin = (unit - 0.5) * 4.0
    totals = [-1.5 + margin / 2, -1.5 - margin / 2]
    counts = [1, 2]
    return [{"choice": choice, "sequence_log_probability": value,
             "mean_token_log_probability": value / count, "token_count": count,
             "tokens": [{"token_id": None, "token_text": f"fixture_token_{i + 1}",
                         "log_probability": value / count, "synthetic_fixture": True}
                        for i in range(count)]}
            for choice, value, count in zip(choices, totals, counts)]


def _score_identity(taxonomy: dict[str, Any], model: dict[str, Any], category: dict[str, Any],
                    variant: dict[str, Any], study: dict[str, Any], backend: str) -> dict[str, Any]:
    prompt = variant["template"].format(skill=category["label"],
                                        role=study["role"]["display_name"],
                                        industry=study["role"]["industry"])
    return {"schema_version": 3, "backend": backend,
            "taxonomy_sha256": taxonomy["taxonomy_sha256"],
            "checkpoint": model["checkpoint"], "revision": model["revision"],
            "tokenizer_checkpoint": model["tokenizer_checkpoint"],
            "tokenizer_revision": model["tokenizer_revision"],
            "architecture": model["architecture"], "prompt_adapter": model["prompt_adapter"],
            "auto_model_class": model["auto_model_class"],
            "chat_template_kwargs": model.get("chat_template_kwargs", {}),
            "dtype": model.get("dtype", "auto"),
            "role": study["role"], "canonical_id": category["canonical_id"],
            "skill_label": category["label"], "prompt_variant": variant,
            "rendered_prompt": prompt, "method": study["scoring"]["method"],
            "choices": study["scoring"]["choices"]}


def _assign_ranks(summary: list[dict[str, Any]], field: str = "within_model_rank") -> None:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summary:
        by_model[row["model_id"]].append(row)
    for values in by_model.values():
        for rank, row in enumerate(sorted(values,
                                          key=lambda x: (-x["mean_model_relative_probability"],
                                                         x["canonical_id"])), 1):
            row[field] = rank


def _add_rank_uncertainty(summary: list[dict[str, Any]], rows: list[dict[str, Any]],
                          iterations: int, level: float) -> None:
    """Bootstrap prompt measurements and retain each skill's rank distribution."""
    successful = [r for r in rows if r.get("status") == "success"]
    summary_index = {(r["model_id"], r["canonical_id"]): r for r in summary}
    for model_id in sorted({r["model_id"] for r in successful}):
        skill_values: dict[str, list[float]] = defaultdict(list)
        for row in successful:
            if row["model_id"] == model_id:
                skill_values[row["canonical_id"]].append(float(row["calibrated_yes_probability"]))
        rng = random.Random(int(digest(["rank-bootstrap", model_id])[:8], 16))
        distributions: dict[str, list[int]] = defaultdict(list)
        for _ in range(iterations):
            means = {skill: statistics.fmean(rng.choices(values, k=len(values)))
                     for skill, values in skill_values.items()}
            for rank, skill in enumerate(sorted(means, key=lambda s: (-means[s], s)), 1):
                distributions[skill].append(rank)
        alpha = (1 - level) / 2
        for skill, ranks in distributions.items():
            ordered = sorted(ranks)
            target = summary_index[(model_id, skill)]
            target["bootstrap_rank_low"] = ordered[int(alpha * (iterations - 1))]
            target["bootstrap_rank_high"] = ordered[int((1 - alpha) * (iterations - 1))]
            target["probability_same_rank_as_point_estimate"] = (
                sum(rank == target["within_model_rank"] for rank in ranks) / len(ranks))


def _discovery_prevalence(taxonomy: dict[str, Any], iterations: int,
                          level: float) -> list[dict[str, Any]]:
    records_by_model: dict[str, set[str]] = defaultdict(set)
    hits: dict[tuple[str, str], set[str]] = defaultdict(set)
    for mapping in taxonomy["mappings"]:
        records_by_model[mapping["model_id"]].add(mapping["record_id"])
        category = next(c["canonical_id"] for c in taxonomy["categories"]
                        if c["label"] == mapping["proposed_canonical"])
        hits[(mapping["model_id"], category)].add(mapping["record_id"])
    result = []
    for model_id, record_ids in sorted(records_by_model.items()):
        ordered = sorted(record_ids)
        for category in taxonomy["categories"]:
            values = [1.0 if record_id in hits[(model_id, category["canonical_id"])] else 0.0
                      for record_id in ordered]
            lo, hi = bootstrap_interval(values, iterations=iterations, level=level,
                                        seed=int(digest(["prevalence", model_id,
                                                         category["canonical_id"]])[:8], 16))
            result.append({"model_id": model_id, "canonical_id": category["canonical_id"],
                           "fixture_data": taxonomy.get("fixture_data", False),
                           "backend": "fixture" if taxonomy.get("fixture_data") else "unknown",
                           "artifact_warning": "SYNTHETIC FIXTURE: not empirical." if taxonomy.get("fixture_data") else "Model elicitation, not labour-market evidence.",
                           "successful_discovery_records": len(values),
                           "records_mentioning_skill": int(sum(values)),
                           "fixture_prevalence": statistics.fmean(values),
                           "ci_low": lo, "ci_high": hi,
                           "uncertainty_type": "descriptive_discovery_record_resampling_interval",
                           "uncertainty_warning": "Resamples the fixed prompt-by-seed discovery records; not a population confidence interval.",
                           "source_record_ids_json": canonical_json(ordered),
                           "fixture_warning": "Synthetic fixture prevalence; not an empirical model or labour-market finding."})
    return result


def score_taxonomy(study_path: Path, models_path: Path, taxonomy_path: Path,
                   approval_path: Path, output_dir: Path, *, backend: str = "transformers") -> dict[str, int]:
    """Run gated, teacher-forced Yes/No sequence scoring on local checkpoints.

    Every cell is separately cached and failures are materialized. The reported
    probability is conditional on the two answer strings, so it is deliberately
    labelled model-relative rather than a cross-tokenizer raw probability.
    """
    taxonomy, approval = require_approval(taxonomy_path, approval_path)
    study, manifest = load_json(study_path), load_json(models_path)
    problems = validate_study(study) + validate_manifest(manifest)
    if problems:
        raise ValueError("invalid configuration:\n- " + "\n- ".join(problems))
    if backend not in {"transformers", "fixture"}:
        raise ValueError(f"unknown scoring backend {backend!r}")
    if backend == "transformers" and approval.get("approval_scope") == "fixture_test":
        raise PermissionError("fixture-test approval cannot authorize real checkpoint scoring")
    score_run_identity = {"schema_version": 2, "backend": backend,
                          "study_config_sha256": digest(study),
                          "model_manifest_sha256": digest(manifest),
                          "taxonomy_sha256": taxonomy["taxonomy_sha256"],
                          "approval_sha256": digest(approval)}
    _reject_incompatible_run(output_dir / "score_run_manifest.json", score_run_identity)
    atomic_json(output_dir / "score_run_manifest.json", {"schema_version": 2, "status": "running",
                "run_identity": score_run_identity, "backend": backend,
                "fixture_data": backend == "fixture", "updated_at": utc_now()})
    torch = transformers = AutoTokenizer = None
    if backend == "transformers":
        try:
            import torch
            import transformers
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("scoring requires: pip install torch transformers accelerate") from exc
    records_dir = output_dir / "records"; records_dir.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    for model_cfg in (m for m in manifest["models"] if m["selected"]):
        tokenizer = model = load_error = None
        if backend == "transformers":
            try:
                tokenizer = AutoTokenizer.from_pretrained(model_cfg["tokenizer_checkpoint"],
                                                          revision=model_cfg["tokenizer_revision"])
                model_class = _auto_model_class(transformers, model_cfg)
                model = model_class.from_pretrained(model_cfg["checkpoint"],
                                                     revision=model_cfg["revision"], device_map="auto",
                                                     dtype=model_cfg.get("dtype", "auto"))
                model.eval()
            except Exception as exc:
                load_error = exc
        for category in taxonomy["categories"]:
            for variant in study["scoring"]["prompt_variants"]:
                prompt = variant["template"].format(skill=category["label"],
                                                     role=study["role"]["display_name"],
                                                     industry=study["role"]["industry"])
                identity = _score_identity(taxonomy, model_cfg, category, variant, study, backend)
                record_id = digest(identity); target = records_dir / f"{record_id}.json"
                if target.exists():
                    cached = load_json(target)
                    if cached.get("cache_identity") != identity:
                        raise ValueError(f"cache identity mismatch in {target}")
                    counts["cached"] += 1; continue
                row = {"schema_version": 3, "record_id": record_id, "cache_identity": identity,
                       "taxonomy_sha256": taxonomy["taxonomy_sha256"],
                       "approval_sha256": digest(approval), "model_id": model_cfg["id"],
                       "checkpoint": model_cfg["checkpoint"], "revision": model_cfg["revision"],
                       "tokenizer_checkpoint": model_cfg["tokenizer_checkpoint"],
                       "tokenizer_revision": model_cfg["tokenizer_revision"],
                       "architecture": model_cfg["architecture"],
                       "prompt_adapter": model_cfg["prompt_adapter"],
                       "auto_model_class": model_cfg["auto_model_class"],
                       "chat_template_kwargs": model_cfg.get("chat_template_kwargs", {}),
                       "requested_dtype": model_cfg.get("dtype", "auto"),
                       "release_date": model_cfg["release_date"],
                       "release_source": model_cfg["release_source"],
                       "cutoff": model_cfg["cutoff"],
                       "canonical_id": category["canonical_id"], "skill_label": category["label"],
                       "prompt_variant": variant["id"], "prompt_template": variant["template"],
                       "rendered_prompt": prompt, "method": study["scoring"]["method"],
                       "choices": study["scoring"]["choices"], "backend": backend,
                       "fixture_data": backend == "fixture", "started_at": utc_now()}
                try:
                    if load_error: raise load_error
                    if backend == "fixture":
                        components = _fixture_components(model_cfg["id"], category["canonical_id"],
                                                        variant["id"], study["scoring"]["choices"])
                        runtime = {"synthetic_fixture": True, "fixture_version": 1,
                                   "warning": "Deterministic mock scores; no checkpoint was loaded or queried."}
                    elif model_cfg["prompt_adapter"] == "chat_template":
                        prompt_ids = _chat_input_ids(tokenizer, prompt, model_cfg).to(model.device)
                    else:
                        prompt_ids = tokenizer(prompt, return_tensors="pt")["input_ids"].to(model.device)
                    if backend == "transformers":
                        components = []
                        for choice in study["scoring"]["choices"]:
                            lp, tokens = _choice_logprob(model, torch, prompt_ids, tokenizer, choice)
                            components.append({"choice": choice, "sequence_log_probability": lp,
                                               "mean_token_log_probability": lp / len(tokens),
                                               "token_count": len(tokens), "tokens": tokens})
                        runtime = {"transformers_version": transformers.__version__,
                                   "torch_version": torch.__version__,
                                   "tokenizer_class": tokenizer.__class__.__name__,
                                   "model_class": model.__class__.__name__,
                                   "auto_model_class": model_cfg["auto_model_class"],
                                   "requested_dtype": model_cfg.get("dtype", "auto"),
                                   "device": str(model.device)}
                    yes, no = components[0]["sequence_log_probability"], components[1]["sequence_log_probability"]
                    yes_mean = components[0]["mean_token_log_probability"]
                    no_mean = components[1]["mean_token_log_probability"]
                    row.update(status="success", components=components,
                               calibrated_yes_probability=choice_probability(yes, no),
                               length_normalized_yes_probability=choice_probability(yes_mean, no_mean),
                               calibration="softmax restricted to the exact Yes/No answer sequences",
                               metric_scope="model_relative_constrained_choice",
                               runtime=runtime)
                    counts["success"] += 1
                except Exception as exc:
                    row.update(status="failure", failure={"type": type(exc).__name__, "message": str(exc)})
                    counts["failure"] += 1
                row["finished_at"] = utc_now(); atomic_json(target, row)
        if backend == "transformers":
            del model, tokenizer
            if torch.cuda.is_available(): torch.cuda.empty_cache()
    rows = [load_json(p) for p in sorted(records_dir.glob("*.json"))]
    outcome_counts = Counter(r.get("status", "unknown") for r in rows)
    summary = summarize_scores(rows, iterations=study["scoring"]["bootstrap_iterations"],
                               level=study["scoring"]["confidence_level"])
    # Within each model, ranks avoid pretending numeric scales are shared.
    _assign_ranks(summary)
    _add_rank_uncertainty(summary, rows, study["scoring"]["bootstrap_iterations"],
                          study["scoring"]["confidence_level"])
    alternative_rows = []
    for row in rows:
        changed = dict(row)
        if changed.get("status") == "success":
            changed["calibrated_yes_probability"] = changed["length_normalized_yes_probability"]
        alternative_rows.append(changed)
    alternative = summarize_scores(alternative_rows,
                                   iterations=study["scoring"]["bootstrap_iterations"],
                                   level=study["scoring"]["confidence_level"])
    _assign_ranks(alternative, "length_normalized_rank")
    alt_index = {(r["model_id"], r["canonical_id"]): r for r in alternative}
    sensitivity = [{"model_id": r["model_id"], "canonical_id": r["canonical_id"],
                    "fixture_data": r["fixture_data"], "backend": r["backend"],
                    "artifact_warning": r["artifact_warning"],
                    "sequence_total_rank": r["within_model_rank"],
                    "length_normalized_rank": alt_index[(r["model_id"], r["canonical_id"])]["length_normalized_rank"],
                    "rank_change": alt_index[(r["model_id"], r["canonical_id"])]["length_normalized_rank"] - r["within_model_rank"],
                    "sequence_total_probability": r["mean_model_relative_probability"],
                    "length_normalized_probability": alt_index[(r["model_id"], r["canonical_id"])]["mean_model_relative_probability"]}
                   for r in summary]
    write_summary_tables(summary, output_dir / "tables")
    atomic_json(output_dir / "tables" / "skill_scores.json", summary)
    _write_csv(output_dir / "tables" / "scoring_method_sensitivity.csv", sensitivity)
    write_svg(summary, output_dir / "plots" / "score_uncertainty_and_ranks.svg")
    _bar_svg([(f'{r["model_id"]} · {r["canonical_id"]}', r["prompt_range"]) for r in summary],
             ("SYNTHETIC FIXTURE — " if backend == "fixture" else "") +
             "Prompt sensitivity (3 fixed variants; descriptive only)",
             output_dir / "plots" / "prompt_sensitivity.svg")
    prevalence = _discovery_prevalence(taxonomy, study["scoring"]["bootstrap_iterations"],
                                       study["scoring"]["confidence_level"])
    _write_csv(output_dir / "tables" / "fixture_discovery_prevalence.csv", prevalence)
    _bar_svg([(f'{r["model_id"]} · {r["canonical_id"]}', r["fixture_prevalence"])
              for r in prevalence], "Synthetic fixture discovery prevalence (not empirical)",
             output_dir / "plots" / "fixture_discovery_prevalence.svg")
    selected = [m for m in manifest["models"] if m["selected"]]
    axis = manifest.get("comparison_axis", "training_period")
    order_key = (lambda x: x["cutoff"]["latest_date"]) if axis == "training_period" else (lambda x: x["release_date"])
    ordered_models = [m["id"] for m in sorted(selected, key=order_key)]
    axis_label = "documented training periods" if axis == "training_period" else "model release generations"
    write_rank_trend_svg(summary, ordered_models, output_dir / "plots" / "rank_trends.svg", axis_label)
    observed = {(r["model_id"], r["canonical_id"], r["prompt_variant"])
                for r in rows if r.get("status") == "success"}
    expected = [(m["id"], c["canonical_id"], v["id"]) for m in selected
                for c in taxonomy["categories"] for v in study["scoring"]["prompt_variants"]]
    missing = [{"fixture_data": backend == "fixture", "backend": backend,
                "artifact_warning": "SYNTHETIC FIXTURE: not empirical." if backend == "fixture" else "Model elicitation, not labour-market evidence.",
                "model_id": m, "canonical_id": c, "prompt_variant": v,
                "reason": "missing_or_failed_scoring_cell"}
               for m, c, v in expected if (m, c, v) not in observed]
    missing_meta = {"fixture_data": backend == "fixture", "backend": backend,
                    "artifact_warning": "SYNTHETIC FIXTURE: not empirical." if backend == "fixture" else "Model elicitation, not labour-market evidence.",
                    "missing_count": len(missing), "rows": missing}
    atomic_json(output_dir / "tables" / "missing_cells.json", missing_meta)
    _write_csv(output_dir / "tables" / "missing_cells.csv", missing or [{
        "fixture_data": backend == "fixture", "backend": backend,
        "artifact_warning": missing_meta["artifact_warning"], "model_id": "",
        "canonical_id": "", "prompt_variant": "", "reason": "no_missing_cells"}])
    model_order = {model_id: i for i, model_id in enumerate(ordered_models)}
    trends = []
    for category in taxonomy["categories"]:
        points = sorted((r for r in summary if r["canonical_id"] == category["canonical_id"]),
                        key=lambda r: model_order[r["model_id"]])
        for previous, current in zip(points, points[1:]):
            trends.append({"fixture_data": current["fixture_data"], "backend": current["backend"],
                           "artifact_warning": current["artifact_warning"],
                           "canonical_id": category["canonical_id"],
                           "earlier_model_id": previous["model_id"],
                           "later_model_id": current["model_id"],
                           "earlier_rank": previous["within_model_rank"],
                           "later_rank": current["within_model_rank"],
                           "rank_change_later_minus_earlier": current["within_model_rank"] - previous["within_model_rank"],
                           "comparison_axis": axis,
                           "interpretation_warning": "Model-order rank change; not historical labour-market demand."})
    _write_csv(output_dir / "tables" / "rank_changes_across_model_periods.csv", trends)
    plan_path = taxonomy_path.parent / "taxonomy_sensitivity_plan.json"
    planned_alternatives = (load_json(plan_path).get("alternatives_requiring_separate_review_and_approval", [])
                            if plan_path.exists() else [])
    atomic_json(output_dir / "taxonomy_sensitivity.json", {
        "fixture_data": backend == "fixture", "backend": backend,
        "artifact_warning": "SYNTHETIC FIXTURE: not empirical." if backend == "fixture" else "Model elicitation, not labour-market evidence.",
        "taxonomy_sha256": taxonomy["taxonomy_sha256"], "approved_taxonomy_version": approval.get("taxonomy_version"),
        "status": "alternative_run_scored" if taxonomy.get("alternative_of_sha256") else "base_run_scored",
        "reason": "Alternatives require separate versioned proposals, approvals, score runs, and compare-taxonomies.",
        "approved_categories_scored": len(taxonomy["categories"]),
        "planned_alternatives": planned_alternatives})
    derived_paths = sorted(p for folder in (output_dir / "tables", output_dir / "plots")
                           for p in folder.glob("*") if p.is_file()) + [output_dir / "taxonomy_sensitivity.json"]
    derived_hashes = {str(p.relative_to(output_dir)): file_sha256(p) for p in derived_paths}
    atomic_json(output_dir / "score_run_manifest.json", {"schema_version": 1, "status": "completed_with_failures" if outcome_counts["failure"] else "completed",
                "taxonomy_sha256": taxonomy["taxonomy_sha256"], "approval_sha256": digest(approval),
                "approved_taxonomy_version": approval.get("taxonomy_version"),
                "backend": backend, "fixture_data": backend == "fixture",
                "run_identity": score_run_identity,
                "study_config": str(study_path), "study_config_sha256": digest(study),
                "model_manifest": str(models_path), "model_manifest_sha256": digest(manifest),
                "scoring_config": study["scoring"], "git_revision": git_revision(),
                "comparison_axis": axis,
                "comparison_axis_warning": manifest.get("comparison_axis_warning"),
                "environment": {"python": sys.version, "platform": platform.platform(),
                                "processor": platform.processor()},
                "derived_artifact_sha256": derived_hashes,
                "counts": dict(outcome_counts), "invocation_counts": dict(counts),
                "missing_success_cells": len(missing),
                "metric_scope": "model_relative_constrained_choice",
                "interpretation_warning": "Model-period differences are not direct labour-market measurements.",
                "updated_at": utc_now()})
    return dict(counts)


def validate_score_artifacts(study_path: Path, models_path: Path, taxonomy_path: Path,
                             approval_path: Path, output_dir: Path) -> list[str]:
    """Recompute identities and derived quantities without loading a checkpoint."""
    errors: list[str] = []
    try:
        taxonomy, approval = require_approval(taxonomy_path, approval_path)
    except (PermissionError, ValueError) as exc:
        return [str(exc)]
    study, model_manifest = load_json(study_path), load_json(models_path)
    manifest_path = output_dir / "score_run_manifest.json"
    if not manifest_path.exists():
        return ["missing artifact: score_run_manifest.json"]
    run_manifest = load_json(manifest_path)
    backend = run_manifest.get("backend")
    expected_run_identity = {"schema_version": 2, "backend": backend,
                             "study_config_sha256": digest(study),
                             "model_manifest_sha256": digest(model_manifest),
                             "taxonomy_sha256": taxonomy["taxonomy_sha256"],
                             "approval_sha256": digest(approval)}
    if run_manifest.get("run_identity") != expected_run_identity:
        errors.append("score run identity does not match current configs/taxonomy/approval")
    if run_manifest.get("study_config_sha256") != digest(study):
        errors.append("score run manifest study config hash mismatch")
    if run_manifest.get("model_manifest_sha256") != digest(model_manifest):
        errors.append("score run manifest model config hash mismatch")
    records = [load_json(p) for p in sorted((output_dir / "records").glob("*.json"))]
    models = {m["id"]: m for m in model_manifest["models"] if m["selected"]}
    categories = {c["canonical_id"]: c for c in taxonomy["categories"]}
    variants = {v["id"]: v for v in study["scoring"]["prompt_variants"]}
    expected_cells = {(m, c, v) for m in models for c in categories for v in variants}
    if len(records) != len(expected_cells):
        errors.append(f"expected {len(expected_cells)} scoring records, found {len(records)}")
    ids: set[str] = set()
    actual_cells: set[tuple[str, str, str]] = set()
    for row in records:
        rid = row.get("record_id")
        if rid in ids:
            errors.append(f"duplicate record_id {rid}")
        ids.add(rid)
        cell = (row.get("model_id"), row.get("canonical_id"), row.get("prompt_variant"))
        actual_cells.add(cell)
        if cell not in expected_cells:
            errors.append(f"{rid}: unexpected scoring cell {cell}")
            continue
        identity = _score_identity(taxonomy, models[cell[0]], categories[cell[1]],
                                   variants[cell[2]], study, backend)
        if row.get("cache_identity") != identity or rid != digest(identity):
            errors.append(f"{rid}: record cache identity mismatch")
        expected_prompt = identity["rendered_prompt"]
        if row.get("rendered_prompt") != expected_prompt or row.get("prompt_template") != variants[cell[2]]["template"]:
            errors.append(f"{rid}: prompt/configuration mismatch")
        if row.get("taxonomy_sha256") != taxonomy["taxonomy_sha256"]:
            errors.append(f"{rid}: taxonomy digest mismatch")
        if row.get("approval_sha256") != digest(approval):
            errors.append(f"{rid}: approval digest mismatch")
        if row.get("status") == "success":
            if len(row.get("components", [])) != 2:
                errors.append(f"{rid}: expected two answer components")
            for component in row.get("components", []):
                if component.get("token_count") != len(component.get("tokens", [])):
                    errors.append(f"{rid}: token component count mismatch")
                token_sum = sum(float(t["log_probability"]) for t in component.get("tokens", []))
                for token in component.get("tokens", []):
                    if "target_logit" in token or "logsumexp_all_vocabulary_logits" in token:
                        if not math.isclose(float(token.get("target_logit", math.nan)) -
                                            float(token.get("logsumexp_all_vocabulary_logits", math.nan)),
                                            float(token["log_probability"]), rel_tol=1e-7, abs_tol=1e-7):
                            errors.append(f"{rid}: target-logit normalization mismatch")
                if not math.isclose(token_sum, float(component.get("sequence_log_probability", math.nan)), rel_tol=1e-10, abs_tol=1e-10):
                    errors.append(f"{rid}: component log-probability sum mismatch")
                expected_mean = token_sum / max(1, len(component.get("tokens", [])))
                if not math.isclose(expected_mean, float(component.get("mean_token_log_probability", math.nan)), rel_tol=1e-10, abs_tol=1e-10):
                    errors.append(f"{rid}: component mean log probability mismatch")
            value = row.get("calibrated_yes_probability")
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                errors.append(f"{rid}: invalid calibrated probability")
            if len(row.get("components", [])) == 2:
                components = row["components"]
                recalibrated = choice_probability(components[0]["sequence_log_probability"],
                                                  components[1]["sequence_log_probability"])
                relength = choice_probability(components[0]["mean_token_log_probability"],
                                               components[1]["mean_token_log_probability"])
                if not math.isclose(recalibrated, float(value), rel_tol=1e-12, abs_tol=1e-12):
                    errors.append(f"{rid}: calibrated probability mismatch")
                if not math.isclose(relength, float(row.get("length_normalized_yes_probability", math.nan)), rel_tol=1e-12, abs_tol=1e-12):
                    errors.append(f"{rid}: length-normalized probability mismatch")
        elif "failure" not in row:
            errors.append(f"{rid}: non-success lacks failure details")
    if actual_cells != expected_cells:
        errors.append("record cell identities do not equal configured Cartesian product")
    required = ["score_run_manifest.json", "tables/skill_scores.csv", "tables/skill_scores.json",
                "tables/scoring_method_sensitivity.csv", "tables/prompt_sensitivity.csv",
                "tables/within_model_rank_trends.csv", "tables/rank_changes_across_model_periods.csv",
                "tables/fixture_discovery_prevalence.csv", "tables/missing_cells.json",
                "tables/missing_cells.csv", "taxonomy_sensitivity.json",
                "plots/score_uncertainty_and_ranks.svg", "plots/prompt_sensitivity.svg",
                "plots/rank_trends.svg", "plots/fixture_discovery_prevalence.svg"]
    for relative in required:
        if not (output_dir / relative).exists():
            errors.append(f"missing artifact: {relative}")
    if run_manifest.get("taxonomy_sha256") != taxonomy["taxonomy_sha256"]:
        errors.append("score run manifest taxonomy digest mismatch")
    if run_manifest.get("fixture_data") and any(not r.get("fixture_data") for r in records):
        errors.append("fixture score run contains an unlabelled record")
    successful = [r for r in records if r.get("status") == "success"]
    actual_outcomes = dict(Counter(r.get("status", "unknown") for r in records))
    if run_manifest.get("counts") != actual_outcomes:
        errors.append("manifest outcome counts mismatch")
    recomputed = summarize_scores(successful, iterations=study["scoring"]["bootstrap_iterations"],
                                  level=study["scoring"]["confidence_level"])
    _assign_ranks(recomputed)
    _add_rank_uncertainty(recomputed, successful, study["scoring"]["bootstrap_iterations"],
                          study["scoring"]["confidence_level"])
    summary_path = output_dir / "tables" / "skill_scores.json"
    if summary_path.exists():
        stored = load_json(summary_path)
        stored_index = {(r["model_id"], r["canonical_id"]): r for r in stored}
        for expected_row in recomputed:
            key = (expected_row["model_id"], expected_row["canonical_id"])
            actual = stored_index.get(key)
            if not actual:
                errors.append(f"missing summary row {key}"); continue
            for field in ("n", "mean_model_relative_probability", "ci_low", "ci_high",
                          "prompt_range", "within_model_rank", "bootstrap_rank_low",
                          "bootstrap_rank_high", "measurement_record_ids_json"):
                if actual.get(field) != expected_row.get(field):
                    errors.append(f"summary {key}: {field} mismatch")
    missing_expected = sorted(expected_cells - {cell for row, cell in
                              ((r, (r.get("model_id"), r.get("canonical_id"), r.get("prompt_variant"))) for r in records)
                              if row.get("status") == "success"})
    missing_path = output_dir / "tables" / "missing_cells.json"
    if missing_path.exists():
        missing_doc = load_json(missing_path)
        stored_missing = sorted((r["model_id"], r["canonical_id"], r["prompt_variant"])
                                for r in missing_doc.get("rows", []))
        if stored_missing != missing_expected or missing_doc.get("missing_count") != len(missing_expected):
            errors.append("missing-cell artifact is inconsistent with scoring records")
        if run_manifest.get("missing_success_cells") != len(missing_expected):
            errors.append("manifest missing-success count mismatch")
    if run_manifest.get("fixture_data"):
        for relative in required:
            path = output_dir / relative
            if not path.exists(): continue
            if path.suffix == ".svg" and "synthetic fixture" not in path.read_text(encoding="utf-8").casefold():
                errors.append(f"fixture plot lacks visible synthetic label: {relative}")
            elif path.suffix == ".csv":
                with path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                if not rows or any(r.get("fixture_data") not in {"True", "true"} or
                                   "SYNTHETIC FIXTURE" not in r.get("artifact_warning", "") for r in rows):
                    errors.append(f"fixture table lacks intrinsic synthetic provenance: {relative}")
    stored_hashes = run_manifest.get("derived_artifact_sha256", {})
    for relative in required:
        if relative == "score_run_manifest.json": continue
        path = output_dir / relative
        if path.exists() and stored_hashes.get(relative) != file_sha256(path):
            errors.append(f"derived artifact provenance hash mismatch: {relative}")
    return errors


def compare_taxonomy_runs(base_taxonomy_path: Path, base_approval_path: Path,
                          base_run_dir: Path, alternative_taxonomy_path: Path,
                          alternative_approval_path: Path, alternative_run_dir: Path,
                          output_dir: Path) -> dict[str, int]:
    """Compare two separately approved and scored taxonomy versions."""
    base_taxonomy, _ = require_approval(base_taxonomy_path, base_approval_path)
    alt_taxonomy, _ = require_approval(alternative_taxonomy_path, alternative_approval_path)
    base_manifest = load_json(base_run_dir / "score_run_manifest.json")
    alt_manifest = load_json(alternative_run_dir / "score_run_manifest.json")
    if base_manifest.get("backend") != alt_manifest.get("backend"):
        raise ValueError("taxonomy sensitivity runs must use the same backend")
    fixture = bool(base_manifest.get("fixture_data") and alt_manifest.get("fixture_data"))
    warning = ("SYNTHETIC FIXTURE: taxonomy sensitivity software verification only; not empirical."
               if fixture else "Model-relative taxonomy sensitivity; not labour-market evidence.")
    base_rows = load_json(base_run_dir / "tables" / "skill_scores.json")
    alt_rows = load_json(alternative_run_dir / "tables" / "skill_scores.json")
    base_index = {(r["model_id"], r["canonical_id"]): r for r in base_rows}
    alt_index = {(r["model_id"], r["canonical_id"]): r for r in alt_rows}
    keys = sorted(set(base_index) | set(alt_index))
    rows = []
    for key in keys:
        left, right = base_index.get(key), alt_index.get(key)
        rows.append({"fixture_data": fixture, "backend": base_manifest["backend"],
                     "artifact_warning": warning, "model_id": key[0], "canonical_id": key[1],
                     "base_present": left is not None, "alternative_present": right is not None,
                     "base_rank": left.get("within_model_rank") if left else None,
                     "alternative_rank": right.get("within_model_rank") if right else None,
                     "rank_change": (right["within_model_rank"] - left["within_model_rank"])
                                    if left and right else None,
                     "base_ci_low": left.get("ci_low") if left else None,
                     "base_ci_high": left.get("ci_high") if left else None,
                     "alternative_ci_low": right.get("ci_low") if right else None,
                     "alternative_ci_high": right.get("ci_high") if right else None,
                     "base_prompt_range": left.get("prompt_range") if left else None,
                     "alternative_prompt_range": right.get("prompt_range") if right else None})
    def directions(run_dir: Path) -> dict[str, int]:
        path = run_dir / "tables" / "rank_changes_across_model_periods.csv"
        if not path.exists(): return {}
        with path.open(newline="", encoding="utf-8") as handle:
            return {r["canonical_id"]: int(r["rank_change_later_minus_earlier"])
                    for r in csv.DictReader(handle)}
    base_direction, alt_direction = directions(base_run_dir), directions(alternative_run_dir)
    direction_rows = [{"fixture_data": fixture, "backend": base_manifest["backend"],
                       "artifact_warning": warning, "canonical_id": skill,
                       "base_rank_direction": base_direction.get(skill),
                       "alternative_rank_direction": alt_direction.get(skill),
                       "direction_changed": (base_direction.get(skill) is not None and
                                             alt_direction.get(skill) is not None and
                                             (base_direction[skill] > 0) - (base_direction[skill] < 0) !=
                                             (alt_direction[skill] > 0) - (alt_direction[skill] < 0))}
                      for skill in sorted(set(base_direction) | set(alt_direction))]
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "taxonomy_score_comparison.csv", rows)
    _write_csv(output_dir / "rank_direction_comparison.csv", direction_rows)
    manifest = {"schema_version": 1, "status": "completed", "fixture_data": fixture,
                "backend": base_manifest["backend"], "artifact_warning": warning,
                "base_taxonomy_sha256": base_taxonomy["taxonomy_sha256"],
                "alternative_taxonomy_sha256": alt_taxonomy["taxonomy_sha256"],
                "base_category_count": len(base_taxonomy["categories"]),
                "alternative_category_count": len(alt_taxonomy["categories"]),
                "base_mapping_coverage": base_taxonomy["coverage"]["mapping_coverage"],
                "alternative_mapping_coverage": alt_taxonomy["coverage"]["mapping_coverage"],
                "comparison_rows": len(rows), "direction_rows": len(direction_rows),
                "compared_quantities": ["coverage", "ranks", "rank directions", "intervals", "prompt ranges"],
                "updated_at": utc_now()}
    atomic_json(output_dir / "comparison_manifest.json", manifest)
    return {"comparison_rows": len(rows), "direction_rows": len(direction_rows)}


def write_summary_tables(summary: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not summary:
        return
    with (output_dir / "skill_scores.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]), lineterminator="\n")
        writer.writeheader(); writer.writerows(summary)
    _write_csv(output_dir / "prompt_sensitivity.csv", [
        {"fixture_data": r["fixture_data"], "backend": r["backend"],
         "artifact_warning": r["artifact_warning"], "model_id": r["model_id"],
         "canonical_id": r["canonical_id"], "prompt_range": r["prompt_range"],
         "n_prompt_measurements": r["n"],
         "uncertainty_warning": r["uncertainty_warning"]} for r in summary])
    _write_csv(output_dir / "within_model_rank_trends.csv", [
        {"fixture_data": r["fixture_data"], "backend": r["backend"],
         "artifact_warning": r["artifact_warning"], "model_id": r["model_id"],
         "canonical_id": r["canonical_id"],
         "within_model_rank": r["within_model_rank"],
         "bootstrap_rank_low": r["bootstrap_rank_low"],
         "bootstrap_rank_high": r["bootstrap_rank_high"],
         "effective_prompt_sample_size": r["effective_prompt_sample_size"],
         "uncertainty_warning": r["uncertainty_warning"],
         "mean_model_relative_probability": r["mean_model_relative_probability"]} for r in summary])


def write_svg(summary: list[dict[str, Any]], path: Path) -> None:
    """Dependency-free uncertainty plot; data remain available in the CSV."""
    if not summary:
        return
    rows = sorted(summary, key=lambda x: (x["model_id"], x["within_model_rank"]))
    width, left, row_h = 920, 330, 24
    height = 70 + row_h * len(rows)
    prefix = "SYNTHETIC FIXTURE — " if all(r.get("fixture_data") for r in summary) else ""
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<style>text{font:12px sans-serif}.title{font:bold 15px sans-serif}.axis{stroke:#999}.ci{stroke:#567}.dot{fill:#174a7e}</style>',
             f'<text class="title" x="10" y="22">{prefix}model-relative scores (descriptive prompt-resampling intervals; n=3)</text>',
             f'<line class="axis" x1="{left}" y1="40" x2="{width-25}" y2="40"/>']
    scale = width - left - 25
    for tick in range(6):
        x = left + scale * tick / 5
        parts.append(f'<text x="{x-8:.1f}" y="36">{tick/5:.1f}</text>')
    for i, row in enumerate(rows):
        y = 60 + i * row_h
        label = f'{row["model_id"]} · {row["canonical_id"]} · rank {row["within_model_rank"]}'
        safe = label.replace("&", "&amp;").replace("<", "&lt;")
        lo, hi, mean = row["ci_low"], row["ci_high"], row["mean_model_relative_probability"]
        parts.extend([f'<text x="10" y="{y+4}">{safe}</text>',
                      f'<line class="ci" x1="{left+lo*scale:.1f}" y1="{y}" x2="{left+hi*scale:.1f}" y2="{y}"/>',
                      f'<circle class="dot" cx="{left+mean*scale:.1f}" cy="{y}" r="4"/>'])
    parts.append('</svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_rank_trend_svg(summary: list[dict[str, Any]], model_ids: list[str], path: Path,
                         axis_label: str = "documented training periods") -> None:
    """Draw within-model rank movement; ranks, not raw scales, share an axis."""
    if len(model_ids) < 2 or not summary:
        return
    index = {(r["model_id"], r["canonical_id"]): r for r in summary}
    skills = sorted({r["canonical_id"] for r in summary})
    width, height, top, bottom = 980, 70 + 28 * len(skills), 55, 25
    left, right = 300, 30
    max_rank = max(r["within_model_rank"] for r in summary)
    xs = {model: left + i * (width - left - right) / (len(model_ids) - 1)
          for i, model in enumerate(model_ids)}
    prefix = "SYNTHETIC FIXTURE — " if all(r.get("fixture_data") for r in summary) else ""
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             '<style>text{font:12px sans-serif}.title{font:bold 15px sans-serif}.line{stroke:#2878b5;fill:none}.dot{fill:#174a7e}</style>',
             f'<text class="title" x="10" y="22">{prefix}within-model rank trends across {axis_label}</text>']
    for model, x in xs.items():
        parts.append(f'<text x="{x - 45:.1f}" y="42">{model}</text>')
    for i, skill in enumerate(skills):
        base = top + i * 28
        parts.append(f'<text x="10" y="{base + 5}">{skill}</text>')
        points = []
        for model in model_ids:
            row = index.get((model, skill))
            if row:
                y = base - 8 + 16 * (row["within_model_rank"] - 1) / max(1, max_rank - 1)
                points.append((xs[model], y, row["within_model_rank"]))
        if len(points) > 1:
            parts.append('<polyline class="line" points="' + " ".join(f'{x:.1f},{y:.1f}' for x, y, _ in points) + '"/>')
        for x, y, rank in points:
            parts.extend([f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3"/>',
                          f'<text x="{x + 6:.1f}" y="{y + 4:.1f}">{rank}</text>'])
    parts.append(f'<text x="10" y="{height-bottom+15}">Ranks are model-relative; movement is not labour-market change.</text>')
    parts.append('</svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
