from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
VALID_CUTOFF = {"documented_cutoff", "documented_period", "inferred", "unknown"}


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            if status not in {"documented_cutoff", "documented_period"}:
                errors.append(f"{where}: selected models require documented cutoff/period")
            if not model.get("revision") or not model.get("tokenizer_revision"):
                errors.append(f"{where}: selected checkpoints and tokenizers must be revision-pinned")
            selected_dates.append(cutoff.get("latest_date", ""))
    if len(selected_dates) < 2 or len(set(selected_dates)) < 2:
        errors.append("at least two selected models with distinct documented periods are required")
    return errors


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


def _record_key(model: dict[str, Any], variant: dict[str, Any], seed: int,
                study: dict[str, Any]) -> str:
    return digest({"checkpoint": model["checkpoint"], "revision": model["revision"],
                   "tokenizer": model["tokenizer_checkpoint"],
                   "tokenizer_revision": model["tokenizer_revision"],
                   "prompt": render_prompt(study, variant), "seed": seed,
                   "generation": study["generation"]})


def _fixture_index(path: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    return {(r["model_id"], r["prompt_variant"], r["seed"]): r for r in json_lines(path)}


def _transformers_generate(model_cfg: dict[str, Any], prompt: str, seed: int,
                           generation: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("install optional inference dependencies: pip install torch transformers accelerate") from exc
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    tokenizer = AutoTokenizer.from_pretrained(model_cfg["tokenizer_checkpoint"],
                                              revision=model_cfg["tokenizer_revision"])
    model = AutoModelForCausalLM.from_pretrained(model_cfg["checkpoint"],
                                                 revision=model_cfg["revision"], device_map="auto")
    if model_cfg["prompt_adapter"] == "chat_template":
        encoded = tokenizer.apply_chat_template([{"role": "user", "content": prompt}],
                                                add_generation_prompt=True, return_tensors="pt")
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
               "model_class": model.__class__.__name__, "device": str(model.device),
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
    fixtures = _fixture_index(fixtures_path) if fixtures_path else {}
    counts = Counter()
    for model in (m for m in manifest["models"] if m["selected"]):
        for variant in study["prompt_variants"]:
            for seed in study["seeds"]:
                key = _record_key(model, variant, seed, study)
                target = records_dir / f"{key}.json"
                if target.exists():
                    counts["cached"] += 1
                    continue
                started = utc_now()
                record: dict[str, Any] = {
                    "schema_version": 1, "record_id": key, "study_id": study["study_id"],
                    "role": study["role"], "model_id": model["id"],
                    "checkpoint": model["checkpoint"], "revision": model["revision"],
                    "tokenizer_checkpoint": model["tokenizer_checkpoint"],
                    "tokenizer_revision": model["tokenizer_revision"],
                    "architecture": model["architecture"], "prompt_adapter": model["prompt_adapter"],
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
        "record_counts": dict(counts), "execution_status": "fixture_demo" if backend == "fixture" else "local_inference"
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
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
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


def approve_taxonomy(taxonomy_path: Path, approval_path: Path, reviewer: str, note: str) -> dict[str, Any]:
    taxonomy = load_json(taxonomy_path)
    if taxonomy.get("status") != "proposed_unapproved":
        raise ValueError("only a proposed_unapproved taxonomy can be approved")
    approval = {"schema_version": 1, "decision": "approved", "taxonomy_sha256": taxonomy["taxonomy_sha256"],
                "reviewer": reviewer, "note": note, "approved_at": utc_now()}
    atomic_json(approval_path, approval)
    return approval


def require_approval(taxonomy_path: Path, approval_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    taxonomy = load_json(taxonomy_path)
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
        out.append({"model_id": model_id, "canonical_id": skill, "n": len(vals),
                    "mean_model_relative_probability": statistics.fmean(vals), "ci_low": lo, "ci_high": hi,
                    "prompt_range": max(vals) - min(vals),
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
        value = float(torch.log_softmax(logits[start + offset], dim=-1)[token_id].item())
        token_rows.append({"token_id": token_id, "token_text": tokenizer.decode([token_id]),
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
                           "successful_discovery_records": len(values),
                           "records_mentioning_skill": int(sum(values)),
                           "fixture_prevalence": statistics.fmean(values),
                           "ci_low": lo, "ci_high": hi,
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
    torch = transformers = AutoModelForCausalLM = AutoTokenizer = None
    if backend == "transformers":
        try:
            import torch
            import transformers
            from transformers import AutoModelForCausalLM, AutoTokenizer
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
                model = AutoModelForCausalLM.from_pretrained(model_cfg["checkpoint"],
                                                             revision=model_cfg["revision"], device_map="auto")
                model.eval()
            except Exception as exc:
                load_error = exc
        for category in taxonomy["categories"]:
            for variant in study["scoring"]["prompt_variants"]:
                prompt = variant["template"].format(skill=category["label"],
                                                     role=study["role"]["display_name"],
                                                     industry=study["role"]["industry"])
                key_data = {"taxonomy": taxonomy["taxonomy_sha256"], "model": model_cfg["id"],
                            "revision": model_cfg["revision"], "skill": category["canonical_id"],
                            "prompt_variant": variant["id"], "choices": study["scoring"]["choices"],
                            "backend": backend}
                record_id = digest(key_data); target = records_dir / f"{record_id}.json"
                if target.exists(): counts["cached"] += 1; continue
                row = {"schema_version": 1, "record_id": record_id, "taxonomy_sha256": taxonomy["taxonomy_sha256"],
                       "approval_sha256": digest(approval), "model_id": model_cfg["id"],
                       "checkpoint": model_cfg["checkpoint"], "revision": model_cfg["revision"],
                       "tokenizer_checkpoint": model_cfg["tokenizer_checkpoint"],
                       "tokenizer_revision": model_cfg["tokenizer_revision"],
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
                        prompt_ids = tokenizer.apply_chat_template([{"role": "user", "content": prompt}],
                                                                  add_generation_prompt=True,
                                                                  return_tensors="pt").to(model.device)
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
             "Prompt sensitivity (max minus min constrained-choice score)",
             output_dir / "plots" / "prompt_sensitivity.svg")
    prevalence = _discovery_prevalence(taxonomy, study["scoring"]["bootstrap_iterations"],
                                       study["scoring"]["confidence_level"])
    _write_csv(output_dir / "tables" / "fixture_discovery_prevalence.csv", prevalence)
    _bar_svg([(f'{r["model_id"]} · {r["canonical_id"]}', r["fixture_prevalence"])
              for r in prevalence], "Synthetic fixture discovery prevalence (not empirical)",
             output_dir / "plots" / "fixture_discovery_prevalence.svg")
    selected = [m for m in manifest["models"] if m["selected"]]
    ordered_models = [m["id"] for m in sorted(selected, key=lambda x: x["cutoff"]["latest_date"])]
    write_rank_trend_svg(summary, ordered_models, output_dir / "plots" / "rank_trends.svg")
    observed = {(r["model_id"], r["canonical_id"], r["prompt_variant"])
                for r in rows if r.get("status") == "success"}
    expected = [(m["id"], c["canonical_id"], v["id"]) for m in selected
                for c in taxonomy["categories"] for v in study["scoring"]["prompt_variants"]]
    missing = [{"model_id": m, "canonical_id": c, "prompt_variant": v,
                "reason": "missing_or_failed_scoring_cell"}
               for m, c, v in expected if (m, c, v) not in observed]
    atomic_json(output_dir / "tables" / "missing_cells.json", missing)
    _write_csv(output_dir / "tables" / "missing_cells.csv", missing)
    model_order = {model_id: i for i, model_id in enumerate(ordered_models)}
    trends = []
    for category in taxonomy["categories"]:
        points = sorted((r for r in summary if r["canonical_id"] == category["canonical_id"]),
                        key=lambda r: model_order[r["model_id"]])
        for previous, current in zip(points, points[1:]):
            trends.append({"canonical_id": category["canonical_id"],
                           "earlier_model_id": previous["model_id"],
                           "later_model_id": current["model_id"],
                           "earlier_rank": previous["within_model_rank"],
                           "later_rank": current["within_model_rank"],
                           "rank_change_later_minus_earlier": current["within_model_rank"] - previous["within_model_rank"],
                           "interpretation_warning": "Model-period rank change; not historical labour-market demand."})
    _write_csv(output_dir / "tables" / "rank_changes_across_model_periods.csv", trends)
    atomic_json(output_dir / "taxonomy_sensitivity.json", {
        "taxonomy_sha256": taxonomy["taxonomy_sha256"], "approved_taxonomy_version": approval.get("taxonomy_version"),
        "status": "counterfactual_taxonomies_not_scored",
        "reason": "Changing approved merges creates a new taxonomy requiring a new digest and human approval. Canonical scores cannot identify split-category scores.",
        "approved_categories_scored": len(taxonomy["categories"]),
        "planned_alternatives": load_json(taxonomy_path.parent / "taxonomy_sensitivity_plan.json")["alternatives_requiring_separate_review_and_approval"]})
    atomic_json(output_dir / "score_run_manifest.json", {"schema_version": 1, "status": "completed_with_failures" if counts["failure"] else "completed",
                "taxonomy_sha256": taxonomy["taxonomy_sha256"], "approval_sha256": digest(approval),
                "approved_taxonomy_version": approval.get("taxonomy_version"),
                "backend": backend, "fixture_data": backend == "fixture",
                "counts": dict(counts), "missing_success_cells": len(missing),
                "metric_scope": "model_relative_constrained_choice",
                "interpretation_warning": "Model-period differences are not direct labour-market measurements.",
                "updated_at": utc_now()})
    return dict(counts)


def validate_score_artifacts(study_path: Path, models_path: Path, taxonomy_path: Path,
                             approval_path: Path, output_dir: Path) -> list[str]:
    """Validate traceability and completeness without loading any checkpoint."""
    errors: list[str] = []
    try:
        taxonomy, approval = require_approval(taxonomy_path, approval_path)
    except (PermissionError, ValueError) as exc:
        return [str(exc)]
    study, manifest = load_json(study_path), load_json(models_path)
    records = [load_json(p) for p in sorted((output_dir / "records").glob("*.json"))]
    expected = len([m for m in manifest["models"] if m["selected"]]) * len(taxonomy["categories"]) * len(study["scoring"]["prompt_variants"])
    if len(records) != expected:
        errors.append(f"expected {expected} scoring records, found {len(records)}")
    ids: set[str] = set()
    for row in records:
        rid = row.get("record_id")
        if rid in ids:
            errors.append(f"duplicate record_id {rid}")
        ids.add(rid)
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
            value = row.get("calibrated_yes_probability")
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                errors.append(f"{rid}: invalid calibrated probability")
        elif "failure" not in row:
            errors.append(f"{rid}: non-success lacks failure details")
    required = ["score_run_manifest.json", "tables/skill_scores.csv", "tables/skill_scores.json",
                "tables/scoring_method_sensitivity.csv", "tables/prompt_sensitivity.csv",
                "tables/within_model_rank_trends.csv", "tables/rank_changes_across_model_periods.csv",
                "tables/missing_cells.json", "taxonomy_sensitivity.json",
                "plots/score_uncertainty_and_ranks.svg", "plots/prompt_sensitivity.svg",
                "plots/rank_trends.svg"]
    for relative in required:
        if not (output_dir / relative).exists():
            errors.append(f"missing artifact: {relative}")
    manifest_path = output_dir / "score_run_manifest.json"
    if manifest_path.exists():
        run_manifest = load_json(manifest_path)
        if run_manifest.get("taxonomy_sha256") != taxonomy["taxonomy_sha256"]:
            errors.append("score run manifest taxonomy digest mismatch")
        if run_manifest.get("fixture_data") and any(not r.get("fixture_data") for r in records):
            errors.append("fixture score run contains an unlabelled record")
    return errors


def write_summary_tables(summary: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not summary:
        return
    with (output_dir / "skill_scores.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
        writer.writeheader(); writer.writerows(summary)
    _write_csv(output_dir / "prompt_sensitivity.csv", [
        {"model_id": r["model_id"], "canonical_id": r["canonical_id"],
         "prompt_range": r["prompt_range"], "n_prompt_measurements": r["n"]} for r in summary])
    _write_csv(output_dir / "within_model_rank_trends.csv", [
        {"model_id": r["model_id"], "canonical_id": r["canonical_id"],
         "within_model_rank": r["within_model_rank"],
         "mean_model_relative_probability": r["mean_model_relative_probability"]} for r in summary])


def write_svg(summary: list[dict[str, Any]], path: Path) -> None:
    """Dependency-free uncertainty plot; data remain available in the CSV."""
    if not summary:
        return
    rows = sorted(summary, key=lambda x: (x["model_id"], x["within_model_rank"]))
    width, left, row_h = 920, 330, 24
    height = 70 + row_h * len(rows)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<style>text{font:12px sans-serif}.title{font:bold 15px sans-serif}.axis{stroke:#999}.ci{stroke:#567}.dot{fill:#174a7e}</style>',
             '<text class="title" x="10" y="22">Model-relative constrained-choice skill scores (95% bootstrap intervals)</text>',
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


def write_rank_trend_svg(summary: list[dict[str, Any]], model_ids: list[str], path: Path) -> None:
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
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             '<style>text{font:12px sans-serif}.title{font:bold 15px sans-serif}.line{stroke:#2878b5;fill:none}.dot{fill:#174a7e}</style>',
             '<text class="title" x="10" y="22">Within-model rank trends across documented model periods</text>']
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
