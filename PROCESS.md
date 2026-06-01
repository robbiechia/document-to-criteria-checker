# Process

## Thought Process
Most thought went into architecture and design decisions to ensure accurate retrieval and safe & accurate generation.

---
### Architecture decisions

I split into two main stages of retrieval and generation, with a RuleSet JSON as the intermediary output. By firstly completing extraction of the entire document first, it allows the user to obtain different granularities of information/code for checks without requerying the LLM. It also allows for a human-in-the-loop review of the extracted ruleset to validate the conditions before it gets turned into code.

The RuleSet JSON as a structured representation of eligibility criteria also allows for more flexible downstream use cases. It contains possible clauses, source references etc., and can be scaled into flowcharts for better visualisation.

### Model choices

**Why Gemini 3.5 Flash for Stage 1?**

The prototype now for both PDFs and infographics with guardrails. Gemini 3.5 Flash seemed the best fit (Ranked 7) because it performs well on multimodal understanding to extract infroamation while remiaining fairly cheap and fast (https://llm-stats.com/leaderboards/best-ai-for-image-understanding). Documents are likely to contain infographics, tables, and formatting that is important for accurate extraction, along with a lot of extra tokens of context. Therefore, the large context window (1M) is beneficial to retain understanding as well.

**Why GPT-5.3-Codex for Stage 2?**

The hybrid Stage 2 generator (deterministic skeleton + LLM review) uses a different model family for the review pass. GPT-5.3-Codex is stronger at code generation which makes it better suited for generating and validating code. It also performs well on reasoning tasks and has low latency, mid-tier cost and good contextual understanding to review the generated code against the source clauses and guardrails. Since we utilise a deterministic skeleton for code generation, we can use a more expensive model for the review pass without blowing up costs.

**Why OpenRouter?**

Single API endpoint for both providers. No separate SDKs to manage, API keys are provider-agnostic, and model strings are swappable in `config.json` without touching code. The cost is a small latency overhead (~50ms) which doesn't matter for a batch extraction task.

---

## Extraction design

The first prototype used a single-call prompt asking the model to simultaneously enumerate conditions, classify them, build the AND/OR tree, and flag escalations. The output was messy: conditions were merged, OR alternatives were flattened to AND, and the model would invent conditions not in the document. The recall was decent but type accuracy and tree structure were poor.

I moved to a chain-of-thought approach with five explicit steps: scope → enumerate → escalation check → classify → JSON output. Critically, escalation detection comes before classification — this stops the model from assigning a clean `threshold` type to a condition that actually requires a database lookup (first-timer status, prior grant history).

The biggest improvement came from worked examples embedded in the prompt. The five cases that caused the most misclassifications:

1. Income averaging (`computation`) vs simple threshold comparison (`threshold`)
2. Record existence checks (`existence`) vs boolean profile fields (`membership`)
3. OR-alternative tiers (income ceilings by household type, grant amounts by citizenship/flat size) vs AND requirements
4. Temporal direction — pre-application wait period (≥ 30 months ago) vs post-purchase deadline (≤ 6 months)
5. External agency data (IRAS AV, CPF income) — always `threshold`/`membership` regardless of where the data comes from, not `more_info_needed`

That last point took iteration to get right. Early prompts led the model to escalate conditions like "Annual Value must not exceed $21,000" because IRAS is involved. The fix: we removed `more_info_needed` as a condition type entirely. If the data lives in an external system, that's a possible data pipeline concern, rather than a condition type, and can always raise discretionary flags. The model now correctly classifies AV checks as `threshold` and CPF income checks as `membership`, and leaves it to the implementation (and input schemas) to figure out where to pull that data from.

I also tried an agentic pipeline which didn't really work well for extraction. The idea was to have separate LLM calls for enumeration, classification, and escalation detection, with the output of each step feeding into the next. It failed to accurate complete tree-building which then produced a malformed JSON that broke stage 2. Validation errors were often present too, which meant the model had to be prompted to fix them rather than just generating correct output in the first place. The extra complexity of managing multiple calls and passing structured data between them also made it more brittle and harder to debug.

---

## Evaluation design

I hand-annotated 85 conditions across four documents. The annotation specifies:
- The verbatim clause from the document
- Condition type (seven-type taxonomy)
- Whether multiple rows under the same category form OR alternatives or AND requirements (`category_logic` column)
- Escalation status

The `category_logic` column came from a bug I caught mid-project: citizenship criteria (applicant must be SC AND co-member must be SC/SPR) and OR alternatives (income tier A or income tier B) were both annotated as multi-row groups. The eval was penalising correct AND extraction as wrong OR structure. Adding an explicit AND/OR flag per group fixed this.

I also accounted for guardrails to ensure that the LLM doesn't just paraphrase and create rules out of nowhere. So I used a fuzzy source-clause matching: compare each annotated condition's verbatim clause against every extracted condition's `source_clause` field using `rapidfuzz.partial_ratio`. A match above 75% is deemed correct.

---

## What I dropped

**Semantic embedding verification**: I tried using `sentence-transformers/all-MiniLM-L6-v2` as a second-tier hallucination check (fuzzy fails → check cosine similarity). The model loaded fine but the scores were misleading — "Annual Value ≤ $21,000" and "Annual Value ≤ $31,000" scored 0.90+ similarity because general-purpose embeddings don't treat specific numbers as semantically distinct. Switched to `token_set_ratio` instead, which is order-insensitive and handles abbreviation expansions cleanly (PR → Permanent Resident, AV → Annual Value).

**Benchmark profiles for Stage 2**: The plan was a `profiles.json` with ~35 boundary-case profiles and expected verdicts, feeding into automated Stage 2 accuracy metrics. I dropped this because the generated code uses field names from the extraction, not a fixed schema — any hardcoded profile would only work for one specific extraction run. Stage 2 eval then became a human review checklist instead to manually assess the code generated.

**`more_info_needed` condition type**: I introduced this type early to handle conditions like private property ownership (whose definition spans nominees, trusts, EC units, mixed-use developments). It turned out the model applied it to anything involving an external agency — income from IRAS, AV from property records, PA card from MSF — which escalated things that are straightforwardly checkable. Removed the type entirely. Everything is either `threshold`, `membership`, `temporal`, `sequential`, `existence`, or `discretionary`. If the data lives in an external system, that's a data pipeline concern, not a condition type.

---


## If I had more time

- Extend the annotation corpus to ten or more documents from different agencies. The current four documents share structural similarities (tiered ceilings, citizenship checks) that may not generalise.
- Add a change-detection layer: when a policy document is re-uploaded, diff the new ruleset against the stored one and surface which conditions changed. This is the highest-value production feature.
- Run a proper ablation comparing `direct`, `cot`, `cot_examples`, and `agentic` variants across all four documents with multiple runs to get variance estimates. The current experiment was single-run.
- Handle multi-document policies: HDB grant eligibility is spread across several PDFs (family grants, singles grants, selling conditions). The current architecture processes one document at a time.
