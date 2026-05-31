"""Shared prompt templates used by all variants."""

STAGE1_USER_TEMPLATE = """
Policy document:
{document_text}

Constraint scenario: {constraint_scenario}

Return a RuleSet JSON object with this shape:
{{
  "policy_name": "string",
  "policy_version": "string",
  "source_document": "filename",
  "constraint_scenario": "string",
  "extraction_variant": "direct",
  "model_used": "string",
  "hints_used": false,
  "chunking_strategy": "full",
  "root": {{
    "rule_id": "ROOT",
    "description": "string",
    "source_clause": "verbatim source text covering the root scope",
    "logic": "AND",
    "conditions": [
      {{
        "rule_id": "HDB-EXAMPLE-AGE",
        "description": "Buyer must be at least 21 years old",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "condition_type": "threshold",
        "field_required": "buyer_age",
        "operator": "gte",
        "threshold": 21
      }},
      {{
        "rule_id": "HDB-EXAMPLE-FIRSTTIMER",
        "description": "Applicant must not have received prior grants",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "condition_type": "existence",
        "escalated": true,
        "escalation_note": "requires record existence check in HDB subsidy history"
      }},
      {{
        "rule_id": "HDB-EXAMPLE-NUCLEUS",
        "description": "Must form valid core family nucleus",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "logic": "OR",
        "conditions": [
          {{
            "rule_id": "HDB-EXAMPLE-NUCLEUS-A",
            "description": "nucleus option A — married couple",
            "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
            "condition_type": "membership",
            "field_required": "nucleus_type",
            "operator": "eq",
            "threshold": "married_couple"
          }}
        ]
      }},
      {{
        "rule_id": "HDB-EXAMPLE-AMOUNT",
        "description": "SC/SC first-timer buying 2-4 room flat receives $80,000 grant",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "condition_type": "computation",
        "entitlement": "$80,000 Family Grant"
      }}
    ]
  }},
  "escalated_rules": [],
  "cot_reasoning": null
}}

Allowed condition_type values: threshold, membership, temporal, computation,
sequential, existence, discretionary, more_info_needed.
Allowed operator values: lte, lt, gte, gt, eq, in, not_in.
Intermediate nodes (AND/OR) must use logic and conditions array. Leaf nodes must use condition_type.
Do not flatten OR alternatives into an AND list.
Set escalated=true for existence, discretionary, and more_info_needed conditions.
Include entitlement field (e.g. "$80,000") on conditions that specify a grant amount or benefit.
"""

RETRY_JSON_ERROR_TEMPLATE = """
Your previous response failed JSON parsing with this error:
{error}

Previous response (first 500 chars):
{previous_response_start}

Please fix the JSON syntax and return ONLY valid JSON, no markdown fences.
Original request: {original_user_message}
"""

RETRY_SCHEMA_ERROR_TEMPLATE = """
Your previous response produced valid JSON but failed schema validation:
{pydantic_error}

Common issues:
- condition_type must be one of: threshold, membership, temporal, computation, composite,
  sequential, history, spatial, discretionary
- operator must be one of: lte, lt, gte, gt, eq, in, not_in
- Leaf nodes must have condition_type set
- Intermediate AND/OR nodes must NOT have condition_type set
- source_clause must be a non-empty verbatim string from the document
- Escalated leaf nodes must have escalated=true

Please correct the issue and return ONLY valid JSON.
Original request: {original_user_message}
"""

# User template for combined path — pdfplumber text as primary context, PDF attached.
# Minimal change from STAGE1_USER_TEMPLATE: the preamble notes the PDF is also attached.
STAGE1_USER_TEMPLATE_PDF_AND_TEXT = """
Policy document (extracted text below; original PDF also attached for layout reference):
{document_text}

Constraint scenario: {constraint_scenario}

The attached PDF is the source of the extracted text above. Use it to verify any
conditions where the text extraction may have lost structure (e.g. tables, indented lists).

Return a RuleSet JSON object with this shape:
{{
  "policy_name": "string",
  "policy_version": "string",
  "source_document": "filename",
  "constraint_scenario": "string",
  "extraction_variant": "direct_pdf_text",
  "model_used": "string",
  "hints_used": false,
  "chunking_strategy": "full",
  "root": {{
    "rule_id": "ROOT",
    "description": "string",
    "source_clause": "verbatim source text covering the root scope",
    "logic": "AND",
    "conditions": [
      {{
        "rule_id": "HDB-EXAMPLE-AGE",
        "description": "Buyer must be at least 21 years old",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "condition_type": "threshold",
        "field_required": "buyer_age",
        "operator": "gte",
        "threshold": 21,
        "ambiguity_flag": false
      }},
      {{
        "rule_id": "HDB-EXAMPLE-NUCLEUS",
        "description": "Must form valid core family nucleus",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "logic": "OR",
        "conditions": [
          {{
            "rule_id": "HDB-EXAMPLE-NUCLEUS-A",
            "description": "nucleus option A — married couple",
            "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
            "condition_type": "membership",
            "field_required": "nucleus_type",
            "operator": "eq",
            "threshold": "married_couple"
          }}
        ]
      }}
    ]
  }},
  "escalated_rules": [],
  "cot_reasoning": null
}}

Allowed condition_type values: threshold, membership, temporal, computation, composite,
sequential, history, spatial, discretionary.
Allowed operator values: lte, lt, gte, gt, eq, in, not_in.
Intermediate nodes (AND/OR) must use logic and conditions array. Leaf nodes must use condition_type.
Do not flatten OR alternatives into an AND list.
source_clause must be the VERBATIM sentence from the document.
"""

# User template for native PDF path — no {document_text} placeholder.
# The PDF is passed as inline base64 content alongside this message.
STAGE1_USER_TEMPLATE_PDF = """
Constraint scenario: {constraint_scenario}

The policy document is attached. Read it directly from the attached file.

Return a RuleSet JSON object with this shape:
{{
  "policy_name": "string",
  "policy_version": "string",
  "source_document": "filename",
  "constraint_scenario": "string",
  "extraction_variant": "direct_pdf",
  "model_used": "string",
  "hints_used": false,
  "chunking_strategy": "full",
  "root": {{
    "rule_id": "ROOT",
    "description": "string",
    "source_clause": "verbatim source text covering the root scope",
    "logic": "AND",
    "conditions": [
      {{
        "rule_id": "HDB-EXAMPLE-AGE",
        "description": "Buyer must be at least 21 years old",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "condition_type": "threshold",
        "field_required": "buyer_age",
        "operator": "gte",
        "threshold": 21,
        "ambiguity_flag": false
      }},
      {{
        "rule_id": "HDB-EXAMPLE-NUCLEUS",
        "description": "Must form valid core family nucleus",
        "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
        "logic": "OR",
        "conditions": [
          {{
            "rule_id": "HDB-EXAMPLE-NUCLEUS-A",
            "description": "nucleus option A — married couple",
            "source_clause": "VERBATIM SENTENCE FROM DOCUMENT",
            "condition_type": "membership",
            "field_required": "nucleus_type",
            "operator": "eq",
            "threshold": "married_couple"
          }}
        ]
      }}
    ]
  }},
  "escalated_rules": [],
  "cot_reasoning": null
}}

Allowed condition_type values: threshold, membership, temporal, computation, composite,
sequential, history, spatial, discretionary.
Allowed operator values: lte, lt, gte, gt, eq, in, not_in.
Intermediate nodes (AND/OR) must use logic and conditions array. Leaf nodes must use condition_type.
Do not flatten OR alternatives into an AND list.
source_clause must be the VERBATIM sentence from the attached document.
"""

# ---------------------------------------------------------------------------
# Stage 2 prompts
# ---------------------------------------------------------------------------

STAGE2_LLM_SYSTEM = """
You are a Python code generator. Given a RuleSet JSON extracted from a Singapore HDB
eligibility policy document, generate executable Python constraint functions.

Structure the generated code as follows:

1. One check function per executable leaf condition (threshold, membership, temporal,
   computation, composite, sequential). Name it check_<rule_id_lower>().
2. One escalation stub per escalated rule. Name it escalate_<rule_id_lower>().
3. A DISPATCH dict mapping rule_id strings to their functions.
4. A _TREE constant encoding the AND/OR tree as nested tuples:
   (rule_id, logic, [(child_id, child_logic, [grandchildren...]), ...])
5. A _eval_node() helper that walks the tree.
6. An evaluate(profile: dict) -> dict function that returns:
   {"overall_verdict": "allow"|"deny"|"escalate",
    "results": [...], "escalated_rule_ids": [...]}

Rules:
- Each check function takes profile: dict[str, Any] and returns RuleResult
- If a required field is missing from profile, return verdict="escalate"
- Include the verbatim source_clause in every function docstring
- Escalation stubs always return verdict="escalate"
- Do NOT import os, sys, subprocess, socket, requests, or any network/file libraries
- Do NOT use eval(), exec(), open(), or __import__()
- Computation conditions: generate a stub that returns escalate with a note explaining
  the formula required, since multi-step formulas cannot be auto-generated

Output ONLY the Python code. No prose, no markdown fences.
"""

STAGE2_LLM_USER_TEMPLATE = """
Generate Python constraint code for this RuleSet:

{ruleset_json}

Additional scenario context: {constraint_scenario}

Follow the structure described in the system prompt exactly.
Include a RuleResult dataclass and all required functions.
"""

STAGE2_HYBRID_REVIEW_SYSTEM = """
You are a Python code reviewer specialising in policy constraint functions.
You will receive deterministically generated Python code alongside the RuleSet JSON
it was derived from.

Review the code for these specific issues:
1. OPERATOR DIRECTION: does the comparison operator match "must not exceed" (lte),
   "at least" (gte), "less than" (lt), "more than" (gt)?
2. THRESHOLD VALUES: does the numeric threshold in the code match the document value exactly?
3. ESCALATION CORRECTNESS: are history, spatial, discretionary conditions returning escalate?
4. MISSING EDGE CASES: are missing profile fields handled (return escalate, not crash)?
5. OR TREE LOGIC: does the evaluate() function correctly allow if ANY OR child passes?

For each issue found, output a corrected version of the affected function.
Preserve all function names exactly — do not rename functions.
Output the COMPLETE corrected Python file, not just the changed functions.

Do NOT add imports for os, sys, subprocess, socket, requests, or network/file libraries.
Output ONLY valid Python code. No prose, no markdown fences.
"""

STAGE2_HYBRID_REVIEW_USER_TEMPLATE = """
RuleSet JSON:
{ruleset_json}

Deterministically generated code:
{deterministic_code}

Review the code against the RuleSet and correct any issues.
Output the complete corrected Python file.
"""

STAGE2_FLOWCHART_SYSTEM = """
You are a diagram generator. Convert the given decision tree JSON into a Mermaid flowchart.

Rules:
- Use flowchart TD (top-down)
- AND nodes: use {diamond shape} with "ALL must pass"
- OR nodes: use {diamond shape} with "ANY must pass"
- Leaf nodes: use [rectangle] with the condition description
- Escalated nodes: use {{hexagon}} with "ESCALATE: description"
- Connect parent to children with -->
- Keep node IDs short and valid Mermaid identifiers (no special chars)
- If the tree has >30 leaf nodes, output only the top 2 levels and add a note "... (truncated)"

Output ONLY the Mermaid diagram starting with "flowchart TD". No prose, no code fences.
"""

STAGE2_FLOWCHART_USER_TEMPLATE = """
Decision tree JSON:
{tree_json}

Generate a Mermaid flowchart for this eligibility decision tree.
"""

STAGE2_TEST_JUDGE_SYSTEM = """
You are a test case reviewer for Python policy constraint functions.
For each test case provided, verify:
1. Is the expected verdict (allow/deny/escalate) correct given the profile?
2. Does the profile actually trigger the condition being tested?
3. Is this a useful boundary test?

For any incorrect test, provide the corrected expected verdict and a brief explanation.
Output JSON: list of {"test_id": ..., "correct": true/false, "correction": ...}.
"""

STAGE2_TEST_JUDGE_USER_TEMPLATE = """
RuleSet:
{ruleset_json}

Test cases to review:
{test_cases_json}

Review each test case and output a JSON array of results.
"""
