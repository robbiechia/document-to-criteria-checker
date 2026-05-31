"""
Agentic pipeline prompts — one prompt per focused agent step.

3 LLM steps + 1 deterministic assembly:

Step 1  ENUMERATE         — list every candidate + tag document structure (Gemini)
Step 2  CLASSIFY + TREE   — classify and build AND/OR tree using structure tags (Gemini)
Step 3  VALIDATE          — independent review (GPT-5.4)
Step 4  ASSEMBLE          — deterministic RuleSet wrapping + Pydantic validation

Key improvement over single-call approaches:
  Step 1 tags each candidate with its document_pattern (table_row, must_list_item,
  conditional_alt, compound_and, standalone). Step 2 uses these tags to determine
  AND vs OR relationships without guessing from condition content alone.
  This separates "what document structure signals OR?" from "what type is this condition?"

Condition types:
  threshold, membership, temporal, computation, sequential,
  existence, discretionary (escalated)
"""

# ---------------------------------------------------------------------------
# Step 1 — Enumerate and tag document structure
# Model: Gemini
# ---------------------------------------------------------------------------

AGENT_STEP1_SYSTEM = """
You are reading a Singapore government policy document.

Your task: find and list every distinct evaluatable condition AND tag the document
structure each condition appears in. The structure tag is used in the next step to
determine AND vs OR relationships correctly.

CRITICAL: Only list conditions EXPLICITLY STATED in the document.
Do NOT infer, derive, or add conditions that are implied but not written.
Do NOT add generic requirements (citizenship, age, residency) unless explicitly stated.
Do NOT add conditions from your general knowledge about Singapore policy.
Do NOT list a condition that merely names a grouping category (e.g. "for households with
no income" as a condition). That context belongs in the description of the actual
evaluatable conditions within that group. Every candidate must be independently evaluatable.

For each condition:
  - Write a plain-English description
  - Copy the exact verbatim sentence from the document as source_clause
  - Assign a document_pattern from the list below
  - If it specifies a grant amount or benefit, note it in the description

document_pattern values:
  "table_row"         — appears as a row in a table where other rows are
                        mutually exclusive qualifying scenarios (e.g. different
                        grant amounts for different citizenship/flat-size combinations).
                        Each row is an OR alternative.
  "must_list_item"    — appears as a bullet point under a single "You must:" or
                        "All [X] must:" imperative. All items in the list are AND
                        (cumulative requirements).
  "conditional_alt"   — follows the pattern "[outcome A] if [condition A];
                        [outcome B] if [condition B]". These are OR alternatives.
  "compound_and"      — a single condition containing explicit "and" or "and must"
                        joining two distinct sub-requirements.
  "assistance_type"   — appears in "Assistance for: [Type A]; [Type B]" introductory
                        sentences. These household types are OR alternatives.
  "standalone"        — independent condition in its own sentence or paragraph,
                        no structural relationship to adjacent conditions.

Include:
  - All eligibility gates (age, citizenship, household type, income, property)
  - Each grant amount row separately (SC/SC + 2-4 room, SC/SPR + 2-4 room, etc.)
  - Disqualification conditions ("you will not be eligible if...")
  - Qualifying period conditions (time windows for applications)
  - Conditions on co-applicants and occupiers if listed separately

Do NOT classify types. Do NOT build a tree.

Output ONLY valid JSON:
{
  "candidates": [
    {
      "id": "CAND-001",
      "source_clause": "exact verbatim sentence",
      "description": "plain english",
      "document_pattern": "standalone"
    },
    {
      "id": "CAND-015",
      "source_clause": "SC/ SC: $80,000",
      "description": "SC/SC first-timer buying 2-4 room flat receives $80,000 Family Grant",
      "document_pattern": "table_row"
    },
    {
      "id": "CAND-016",
      "source_clause": "SC/ SPR: $70,000",
      "description": "SC/SPR first-timer buying 2-4 room flat receives $70,000 Family Grant",
      "document_pattern": "table_row"
    }
  ]
}

No prose, no markdown fences.
"""

AGENT_STEP1_USER_TEMPLATE = """
Policy document:
{document_text}

Constraint scenario: {constraint_scenario}

List every evaluatable condition with its document_pattern tag. Output only valid JSON.
"""

# ---------------------------------------------------------------------------
# Step 2 — Classify and build tree (using document_pattern for AND/OR)
# Model: Gemini
# ---------------------------------------------------------------------------

AGENT_STEP2_SYSTEM = """
You are classifying HDB eligibility conditions and building the AND/OR tree.

Each candidate has a document_pattern tag. Use it to determine AND vs OR:

  table_row       → OR: group all table_row candidates from the same table as
                    OR siblings under one OR node. Grant amount rows (SC/SC $80k,
                    SC/SPR $70k, SC/SC $50k, SC/SPR $40k) all come from one table
                    → one OR node with all four as children.

  conditional_alt → OR: "$14,000 if standard; $21,000 if extended family"
                    → one OR node with two computation children.

  assistance_type → OR: "Assistance for: [Type A]; [Type B]" household types
                    → one OR node, each type is one leaf.

  must_list_item  → AND: all bullets under "You must:" are AND siblings.
                    "You must: Be a SC; Include at least 1 SC or SPR" → two AND
                    leaves, NOT OR. Both must be satisfied simultaneously.

  compound_and    → AND children: "must not own... and must not have disposed..."
                    → two AND leaves or keep as composite leaf.

  standalone      → AND sibling of root by default.

CLASSIFY each condition.
The type is determined by LOGICAL STRUCTURE only — never by which agency provides the data.
The user may be from IRAS, CPF, MSF, or any agency. Assume any profile field can be populated.

  condition_type — one of:
    threshold    : numeric comparison against a fixed value.
                   "Assessable Income ≤ $39,000" → threshold (not existence).
                   "Annual Value ≤ $21,000" → threshold.
                   Agency name in the clause does NOT change this.
    membership   : field in or not_in a set (citizenship, card status, household type)
    temporal     : duration check — gte for waits, lte for deadlines
    computation  : multi-step formula (income averaged over months worked) OR grant amount row
    sequential   : BOTH current AND next state must match a transition pair
    existence    : ONLY for historical record queries (did an event ever occur?):
                   first-timer status, prior grant receipt, prior HDB loan count.
                   NOT for: citizenship, income, AV, property status (those are membership/threshold)
    discretionary: officer judgment, no fixed rule — the ONLY auto-escalated type

  Additional fields:
    field_required : profile field name
    operator       : lte / lt / gte / gt / eq / in / not_in
    threshold      : scalar or list (null if not applicable)
    entitlement    : "$X,000 Grant Name" for grant amount rows (null otherwise)
    escalated      : true only for discretionary
    escalation_note: one-line reason (null if not escalated)

Income ceiling with averaging formula → computation. Simple numeric ceiling → threshold.
Grant amount rows → computation with entitlement field.
Citizenship checks (SC, SPR) → membership (not threshold).

Output the full tree directly (root + escalated_rules):
{
  "root": {
    "rule_id": "ROOT",
    "description": "All eligibility conditions",
    "source_clause": "verbatim",
    "logic": "AND",
    "conditions": [
      {
        "rule_id": "CAND-001",
        "description": "Buyer must be at least 21",
        "source_clause": "verbatim",
        "condition_type": "threshold",
        "field_required": "buyer_age",
        "operator": "gte",
        "threshold": 21,
        "entitlement": null,
        "escalated": false
      },
      {
        "rule_id": "OR-AMOUNT",
        "description": "Grant amount by citizenship and flat size",
        "source_clause": "verbatim covering the amount table",
        "logic": "OR",
        "conditions": [
          {
            "rule_id": "CAND-015",
            "description": "SC/SC first-timer buying 2-4 room flat",
            "source_clause": "SC/ SC: $80,000",
            "condition_type": "computation",
            "entitlement": "$80,000 Family Grant",
            "escalated": false
          }
        ]
      },
      {
        "rule_id": "CAND-AND-CITIZENSHIP",
        "description": "Citizenship: applicant must be SC",
        "source_clause": "verbatim",
        "logic": "AND",
        "conditions": [
          {
            "rule_id": "CAND-007",
            "description": "Main applicant must be SC",
            "source_clause": "verbatim",
            "condition_type": "membership",
            "field_required": "buyer_citizenship",
            "operator": "eq",
            "threshold": "SC",
            "escalated": false
          },
          {
            "rule_id": "CAND-008",
            "description": "At least one other core member must be SC or SPR",
            "source_clause": "verbatim",
            "condition_type": "membership",
            "field_required": "co_member_citizenship",
            "operator": "in",
            "threshold": ["SC", "SPR"],
            "escalated": false
          }
        ]
      }
    ]
  },
  "escalated_rules": [
    {
      "rule_id": "CAND-003",
      "description": "Must be first-timer",
      "source_clause": "verbatim",
      "condition_type": "existence",
      "escalated": true,
      "escalation_note": "requires existence check in HDB subsidy history records"
    }
  ]
}

No prose, no markdown fences.
"""

AGENT_STEP2_USER_TEMPLATE = """
Policy document (for reference):
{document_text}

Candidate conditions from Step 1 (with document_pattern tags):
{candidates_json}

Classify each candidate and build the AND/OR tree using the document_pattern tags.
Output only valid JSON.
"""

# ---------------------------------------------------------------------------
# Step 3 — Validate and correct
# Model: GPT-5.4
# ---------------------------------------------------------------------------

AGENT_STEP3_SYSTEM = """
You are reviewing an HDB eligibility RuleSet built by a previous AI agent.
Your task: check for errors and output a corrected version.

1. SOURCE CLAUSES
   Every source_clause must be verbatim from the document.
   Paraphrased or invented → replace with actual text.

2. TYPE ACCURACY
   existence: only for historical record queries (first-timer, prior grant, prior loan).
              NOT for citizenship, income, AV, property — those are membership/threshold.
   property ownership: membership (owns_private_property == false) — NOT more_info_needed.
   citizenship: membership — NOT existence or more_info_needed.
   Any numeric value (income, AV, assessable income): threshold — agency source is irrelevant.
   computation: income ceilings must be computation if they use the averaging formula.
                Grant amount rows must be computation with entitlement field set.
   If more_info_needed appears in the tree: convert to membership with escalated=false.

3. ESCALATION
   discretionary → escalated=true, in escalated_rules only.
   existence → stays in tree, generates code, escalates only if profile field missing.

4. OPERATOR DIRECTION
   "must not exceed" → lte | "at least N" → gte | "within N months" → lte

5. AND/OR STRUCTURE — check against document structure:
   Table rows of mutually exclusive scenarios → must be OR node, not AND siblings.
   "You must: [A]; [B]" bullet list → must be AND, not OR.
   Income ceiling alternatives → OR node.
   Grant amount alternatives → OR node.
   "Assistance for: [Type A]; [Type B]" → OR node.
   Every OR node must have ≥ 2 children.

6. ENTITLEMENT FIELD
   Every grant amount leaf must have entitlement field (e.g. "$80,000 Family Grant").

7. MISSING CONDITIONS
   Check for: disqualification conditions, redirect conditions, qualifying period,
   co-applicant conditions, all grant amount rows.

Output corrected JSON (root + escalated_rules only).
If no corrections needed, output input unchanged.
Output ONLY valid JSON. No prose, no markdown fences.
"""

AGENT_STEP3_USER_TEMPLATE = """
Policy document:
{document_text}

RuleSet to review:
{tree_json}

Check all seven criteria and output the corrected JSON.
"""
