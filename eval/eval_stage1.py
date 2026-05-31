"""Evaluate Stage 1 extraction against the manually annotated criterias corpus.

Corpus structure (criterias.csv):
  - Key: (clause_ref, Category) — rows with the same key are OR alternatives
  - condition_type may be compound e.g. "membership, existence"
    → type_match if extracted type matches ANY of the listed types
  - escalated: "TRUE" / "FALSE" (uppercase strings)
  - No ambiguity columns — replaced by free-text notes column
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
import sys

from rapidfuzz import fuzz

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------

def load_corpus(csv_path: str, doc_id: str | None = None) -> list[dict]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for idx, row in enumerate(csv.DictReader(f)):
            if not (row.get("clause_verbatim") or "").strip():
                continue
            if doc_id and row.get("doc_id") != doc_id:
                continue
            row["_corpus_id"] = (
                f"{row.get('clause_ref', '?')} | "
                f"{row.get('Category', '?')} | "
                f"row{idx}"
            )
            row["_or_group_key"] = (
                row.get("clause_ref", "").strip(),
                row.get("Category", "").strip(),
            )
            # Compound types e.g. "membership, existence" → {"membership", "existence"}
            raw_type = (row.get("condition_type") or "").strip()
            row["_type_set"] = (
                {t.strip() for t in raw_type.split(",") if t.strip()}
                if raw_type else set()
            )
            row["_escalated"] = row.get("escalated", "").upper() == "TRUE"
            # category_logic: AND groups are cumulative requirements, OR groups are alternatives
            row["_category_logic"] = row.get("category_logic", "OR").strip().upper() or "OR"
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Ruleset traversal
# ---------------------------------------------------------------------------

def collect_leaf_nodes(ruleset_data: dict) -> list[dict]:
    leaves: list[dict] = []

    def walk(node: dict) -> None:
        children = node.get("conditions") or []
        if not children and node:
            leaves.append(node)
        for child in children:
            walk(child)

    walk(ruleset_data.get("root", {}))
    for rule in ruleset_data.get("escalated_rules", []):
        walk(rule)
    return leaves


def build_parent_map(ruleset_data: dict) -> dict[str, tuple[str, str]]:
    parent_map: dict[str, tuple[str, str]] = {}

    def walk(node: dict, parent_id: str | None, parent_logic: str) -> None:
        rule_id = node.get("rule_id", "")
        if parent_id is not None and rule_id:
            parent_map[rule_id] = (parent_id, parent_logic)
        for child in node.get("conditions", []):
            walk(child, rule_id, node.get("logic", "AND"))

    walk(ruleset_data.get("root", {}), None, "AND")
    for rule in ruleset_data.get("escalated_rules", []):
        walk(rule, None, "AND")
    return parent_map


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_conditions_to_corpus(
    ruleset_data: dict,
    corpus: list[dict],
    match_threshold: int = 75,
) -> list[dict]:
    extracted = collect_leaf_nodes(ruleset_data)
    matches = []

    for row in corpus:
        corpus_clause = (row.get("clause_verbatim") or "").strip()
        type_set = row["_type_set"]
        corpus_escalated = row["_escalated"]

        best_score = 0.0
        best_node: dict | None = None
        for node in extracted:
            sc = (node.get("source_clause") or "").strip()
            if not sc:
                continue
            score = fuzz.partial_ratio(corpus_clause, sc)
            if score > best_score:
                best_score = score
                best_node = node

        matched = best_score >= match_threshold
        extracted_type = best_node.get("condition_type") if best_node else None
        extracted_escalated = bool((best_node or {}).get("escalated", False))

        # Type match: extracted type must appear in the corpus type set
        type_match = matched and bool(type_set) and extracted_type in type_set

        matches.append({
            "corpus_id": row["_corpus_id"],
            "or_group_key": row["_or_group_key"],
            "_category_logic": row.get("_category_logic", "OR"),
            "corpus_type_set": type_set,
            "has_corpus_type": bool(type_set),
            "corpus_escalated": corpus_escalated,
            "matched": matched,
            "match_score": best_score,
            "extracted_rule_id": best_node.get("rule_id") if best_node else None,
            "extracted_type": extracted_type,
            "extracted_escalated": extracted_escalated,
            "type_match": type_match,
        })
    return matches


# ---------------------------------------------------------------------------
# OR group accuracy
# ---------------------------------------------------------------------------

def eval_or_group_accuracy(matches: list[dict], ruleset_data: dict) -> dict:
    """OR structure check for OR groups; recall check for AND groups.

    Only groups with category_logic=OR are checked for OR tree structure.
    Groups with category_logic=AND (e.g. citizenship criteria, property ownership)
    are cumulative requirements — checking whether all were found (AND recall),
    not whether they appear as OR siblings.
    """
    parent_map = build_parent_map(ruleset_data)

    # Resolve category_logic per group key
    group_logic: dict[tuple, str] = {}
    for m in matches:
        key = m["or_group_key"]
        if key not in group_logic:
            group_logic[key] = m.get("_category_logic", "OR")

    group_corpus_counts: dict[tuple, int] = defaultdict(int)
    for m in matches:
        group_corpus_counts[m["or_group_key"]] += 1

    group_matched_ids: dict[tuple, list[str]] = defaultdict(list)
    group_matched_count: dict[tuple, int] = defaultdict(int)
    for m in matches:
        if m["matched"]:
            group_matched_count[m["or_group_key"]] += 1
            if m["extracted_rule_id"]:
                group_matched_ids[m["or_group_key"]].append(m["extracted_rule_id"])

    # Separate OR and AND multi-row groups
    or_groups = {k: v for k, v in group_corpus_counts.items()
                 if v > 1 and group_logic.get(k, "OR") == "OR"}
    and_groups = {k: v for k, v in group_corpus_counts.items()
                  if v > 1 and group_logic.get(k, "OR") == "AND"}

    # OR group accuracy: ≥2 matched nodes share a common OR parent
    total_or_groups = len(or_groups)
    correct_or_groups = 0
    or_group_details = []

    for key, corpus_count in or_groups.items():
        extracted_ids = group_matched_ids.get(key, [])
        matched_count = len(extracted_ids)
        or_preserved = False

        if matched_count >= 2:
            or_parent_tally: dict[str, int] = defaultdict(int)
            for rid in extracted_ids:
                parent_id, parent_logic = parent_map.get(rid, (None, None))
                if parent_logic == "OR" and parent_id:
                    or_parent_tally[parent_id] += 1
            if any(count >= 2 for count in or_parent_tally.values()):
                or_preserved = True

        if or_preserved:
            correct_or_groups += 1

        or_group_details.append({
            "group": f"{key[0]} | {key[1]}",
            "logic": "OR",
            "corpus_alternatives": corpus_count,
            "matched_alternatives": matched_count,
            "or_preserved": or_preserved,
        })

    # AND group recall: all rows in group must be matched
    total_and_groups = len(and_groups)
    complete_and_groups = 0
    and_group_details = []

    for key, corpus_count in and_groups.items():
        matched_count = group_matched_count.get(key, 0)
        all_found = matched_count == corpus_count
        if all_found:
            complete_and_groups += 1
        and_group_details.append({
            "group": f"{key[0]} | {key[1]}",
            "logic": "AND",
            "corpus_conditions": corpus_count,
            "matched_conditions": matched_count,
            "all_found": all_found,
        })

    return {
        "or_group_accuracy": correct_or_groups / total_or_groups if total_or_groups else 1.0,
        "correct_or_groups": correct_or_groups,
        "total_or_groups": total_or_groups,
        "or_group_details": or_group_details,
        "and_group_recall": complete_and_groups / total_and_groups if total_and_groups else 1.0,
        "complete_and_groups": complete_and_groups,
        "total_and_groups": total_and_groups,
        "and_group_details": and_group_details,
    }


# ---------------------------------------------------------------------------
# Main eval
# ---------------------------------------------------------------------------

def eval_stage1(ruleset_path: str, corpus: list[dict], label: str, out_dir: str = "eval/results") -> dict:
    ruleset_data = json.loads(Path(ruleset_path).read_text(encoding="utf-8"))
    matches = match_conditions_to_corpus(ruleset_data, corpus)

    total = len(matches)
    found = [m for m in matches if m["matched"]]

    typed_found = [m for m in found if m["has_corpus_type"]]
    type_correct = [m for m in typed_found if m["type_match"]]

    corpus_escalated = [m for m in matches if m["corpus_escalated"]]
    escalated_found = [m for m in corpus_escalated if m["extracted_escalated"]]

    or_result = eval_or_group_accuracy(matches, ruleset_data)

    hallucination_count = ruleset_data.get("hallucination_risk_count", 0)
    extracted_total = len(collect_leaf_nodes(ruleset_data))

    results = {
        "label": label,
        "model": ruleset_data.get("model_used", "unknown"),
        "variant": ruleset_data.get("extraction_variant", "unknown"),
        "hints_used": ruleset_data.get("hints_used", False),
        "chunking": ruleset_data.get("chunking_strategy", "full"),
        "condition_recall": len(found) / total if total else 0,
        "conditions_found": len(found),
        "conditions_total": total,
        "conditions_missed": [m["corpus_id"] for m in matches if not m["matched"]],
        "type_accuracy": len(type_correct) / len(typed_found) if typed_found else 0,
        "type_correct": len(type_correct),
        "typed_matched_total": len(typed_found),
        "type_mismatches": [
            {
                "corpus_id": m["corpus_id"],
                "expected": sorted(m["corpus_type_set"]),
                "got": m["extracted_type"],
            }
            for m in typed_found if not m["type_match"]
        ],
        "escalation_recall": (
            len(escalated_found) / len(corpus_escalated) if corpus_escalated else 0
        ),
        "escalated_found": len(escalated_found),
        "escalated_total": len(corpus_escalated),
        "escalated_missed": [
            m["corpus_id"] for m in corpus_escalated if not m["extracted_escalated"]
        ],
        **or_result,
        "hallucination_count": hallucination_count,
        "hallucination_rate": (
            hallucination_count / extracted_total if extracted_total else 0
        ),
    }

    out_path = Path(out_dir) / f"{label}_stage1.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"\n=== Stage 1 Eval: {label} ===")
    print(
        f"Model: {results['model']} | "
        f"Variant: {results['variant']} | "
        f"Hints: {results['hints_used']}"
    )
    print(
        f"Condition recall:  {results['condition_recall']:.1%} "
        f"({results['conditions_found']}/{results['conditions_total']})"
    )
    print(
        f"Type accuracy:     {results['type_accuracy']:.1%} "
        f"({results['type_correct']}/{results['typed_matched_total']} typed)"
    )
    print(
        f"Escalation recall: {results['escalation_recall']:.1%} "
        f"({results['escalated_found']}/{results['escalated_total']})"
    )
    print(
        f"OR group accuracy: {results['or_group_accuracy']:.1%} "
        f"({results['correct_or_groups']}/{results['total_or_groups']} OR groups)"
    )
    print(
        f"AND group recall:  {results['and_group_recall']:.1%} "
        f"({results['complete_and_groups']}/{results['total_and_groups']} AND groups fully found)"
    )
    print(
        f"Hallucination rate: {hallucination_count}/{extracted_total} "
        f"({results['hallucination_rate']:.1%})"
    )

    if results["conditions_missed"]:
        print(f"\nMissed ({len(results['conditions_missed'])}):")
        for cid in results["conditions_missed"]:
            print(f"  - {cid}")

    if results["type_mismatches"]:
        print(f"\nType mismatches ({len(results['type_mismatches'])}):")
        for tm in results["type_mismatches"]:
            print(f"  - {tm['corpus_id']}: expected {tm['expected']}, got {tm['got']!r}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ruleset", required=True)
    parser.add_argument("--corpus", default="data/annotation/annotations.csv")
    parser.add_argument("--doc-id", default=None)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out-dir", default="eval/results",
                        help="Directory for output files (default: eval/results)")
    args = parser.parse_args()

    corpus = load_corpus(args.corpus, doc_id=args.doc_id)
    if args.doc_id:
        print(f"Filtered to doc_id='{args.doc_id}': {len(corpus)} rows")

    eval_stage1(args.ruleset, corpus, args.label, out_dir=args.out_dir)
