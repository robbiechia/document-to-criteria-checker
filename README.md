# Document-to-Criteria Checker

## Motivation:
I recently learned that HDB Flat elibility letters at times were delayed because of cross-agency data privacy concerns and manual eligibility checks. The many different requirements for stuff were documented on the website but it was hard to grasp and internalise them. Perhaps within the agency, they have the official length document where everything sits there but it is dense and logic is not explicitly spelled out at times. A good amount of discretion and scrutiny is crucial for public policy checks too

Instinctively, multi-step workflows or LLMs are the go-to solution today to internalise these texts, but difficulties always lie in dealing with user data. We shouldn't parse private data profiles, so this project explores the alternative angle to utilise LLMs to understand policy documents and retrieve key constraints/elgibility critierias, and then use that to generate executable code that can be run in-house against applicant data. No LLM sees any personal data, only the public policy document.

## Why this proposed solution:
1) Firstly, the ability to automate extraction of public policy rules using tech to simplify and help stakeholders visualise & obtain key information about the specific components of the policies - requirements or mixtures of conditions.
2) Secondly, the strength of LLMs also lie in generation, so we tackle privacy by developing a framework to help perform checks efficiently without seeing the data first. Data lies in different forms - excels, databases etc. but logic is still generalisable. Code is also deterministic and can be reviewed and audited for greater clarity, so this is an small prototype which I believe can be scalable into a general use case across different agencies and policies.

## What data I used:
I focused on Singapore government eligibility schemes (citizenship, grants, subsidies, healthcare assistance) which were largely embedded into their websites as tables for public consumption. I saved the entire webpage as a PDF and also used Gemini to create an infographic version of the same content. I chose not to scrape the webpages because the exact format of webpages itself are likely what one would expect in policy documents (typically PDFs). I also hand-annotated and extracted 85 conditions across the four documents for evaluation as a small ground truth corpus.

## How it works:

This is the scope of the architecture:

```
Input: Policy PDF / Infographic
        │
Stage 1a: Extraction of text data and filtering for obligation clauses (words that infer requirements)
- pdfplumber text extraction + obligation-clause filtering
        │
Stage 1a: LLM extraction of conditions + AND/OR logic
- LLM extracts conditions, classifies into types
- I explored different techniques (Chain-of-thought, direct prompt, few-shot Chain-of-thought, agentic workflow) but best was few-shot chain-of-thought
        │
Stage 1b: Guardrails & evals
- Pydantic validation + retry (for schema compliance)
- Two-tier source clause guardrail (fuzzy match + token set ratio > 75% / 70% to prevent hallucinations)
        │
Stage 1c: Output structured RuleSet JSON
- Each condition has type, source clause, escalation flag, AND/OR tree position
        │
Stage 2a: Code generation
- Three variants: deterministic template, LLM generation, hybrid (deterministic skeleton + LLM review pass)
- Chose hybrid for current use case
- Optional schema field mapping
- Guardrails: syntax check, dangerous call pattern check, function coverage check, presence of evaluate()
        │
Stage 2b: Output executable Python or SQL
- Option to input schemas for reference
        │
        ▼
Output: Executable Python or SQL
```

The policy document goes through pdfplumber (text extraction and obligation-clause filtering) before reaching the LLM. For infographics, Gemini reads the image natively. The model is given a chain-of-thought prompt with worked examples covering the most common classification mistakes: income averaging vs simple threshold, record existence checks vs field membership, OR-alternative tiers vs AND requirements, temporal direction (wait period vs deadline).

In Stage 1, the LLM aims to extract:
- A condition type (`threshold`, `membership`, `temporal`, `computation`, `sequential`, `existence`, `discretionary`)
- The verbatim source clause from the document
- An AND/OR tree position
- An escalation flag for conditions that cannot generate executable logic (discretionary officer judgment only)

A two-tier guardrail verifies every source clause against the extracted document text — first fuzzy string match (≥75%), then token-set ratio (≥70%) for cases where the model expanded abbreviations. Conditions failing both are flagged for review.

In Stage 2, the RuleSet drives code generation. my generator variants are:
- **Deterministic**: templates walk the tree and emit one function per condition. Fast, no API call, but schema-unaware.
- **LLM**: sends the full ruleset to a code-generation model.
- **Hybrid** : deterministic skeleton + LLM review pass that fixes operator directions after the skeleton is applied, maps to the user's database schema, and adds formula context for computation conditions.

## Results

** Stage 1 Extraction: **

Few-shot Chain-of-thought (cot_examples) was the best extraction variant, with 99% recall, 92% type accuracy, 71% escalation recall, 33% OR group accuracy, and 2% hallucination rate. The deterministic generator + GPT review pass produced accurate code with correct operator directions and threshold values in all tested cases

┌──────────────┬────────┬──────────┬────────────┬────────┬──────┐
│   Variant    │ Recall │ Type acc │ Esc recall │ OR acc │ Hall │
├──────────────┼────────┼──────────┼────────────┼────────┼──────┤
│ direct       │ 96%    │ 73%      │ 71%        │ 47%    │ 0%   │
├──────────────┼────────┼──────────┼────────────┼────────┼──────┤
│ cot          │ 95%    │ 84%      │ 50%        │ 29%    │ 5%   │
├──────────────┼────────┼──────────┼────────────┼────────┼──────┤
│ cot_examples │ 99%    │ 92%      │ 71%        │ 33%    │ 2%   │
└──────────────┴────────┴──────────┴────────────┴────────┴──────┘

** Stage 2 Generation: **

This was not the most extensive human-validation check covered by me, as the condition sets tested against were fairly simple and with validation guardrails in place. Hence mostly sound code was generated across variants (except pure LLM because it lacked a lot of schema context that deterministic outputs supported), so I set it as the hybrid generator to account for schema context and to leverage on the LLM's strength in generation while keeping the deterministic skeleton for safety. 

---

## Setup

### Prerequisites
- Docker Desktop installed and running
- An [OpenRouter](https://openrouter.ai) API key 

### Steps

**1. Clone the repo**
```bash
git clone https://github.com/robbiechia/document-to-criteria-checker.git

cd document-to-criteria-checker
```
**2. Get the data**

Download the `data/` folder from this shared Google Drive [link](https://drive.google.com/drive/folders/1Bu855ez8YD3gf5DrX3L0x9w5idsJLvv-?usp=sharing) and place it at the project root. The folder contains:
```
data/
  annotation/        ← my hand-annotated condition corpus (CSV)
  pdf/                ← policy PDFs
  infographic/        ← infographic PNGs
  demo_schema_singapore_citizenship.sql   ← demo SQL schema for Stage 2 generation
```
**3. Set up environment variables**
```bash
cp .env.example .env
```

**4. Set your OPENROUTER_API_KEY **
```bash
echo "OPENROUTER_API_KEY=sk-or-..." > .env
```

or manually edit the `.env` file to include your OpenRouter API key.

**5. Build and run**
```bash
docker compose build       # one-time, ~3 min
docker compose up app      # → http://localhost:8501

Open the Streamlit UI in your browser via http://localhost:8501
```

### Running evaluations (CLI)

Run Stage 1 on any document — it extracts conditions, evaluates against the annotation corpus if one exists for that document, and saves results to `eval/results/`.

```bash
# Extract from the Singapore Citizenship PDF
docker compose run --rm eval stage1 \
    --pdf data/pdf/ica_singapore_citizenship_eligibility.pdf \
    --scenario "What are the eligibility criteria to become a Singapore Citizen?"

# Generate Python or SQL from the extracted ruleset
docker compose run --rm eval stage2 \
    --ruleset eval/results/<your_label>_ruleset.json \
    --language python

# Generate SQL with the demo schema (maps field names to database columns)
docker compose run --rm eval stage2 \
    --ruleset eval/results/<your_label>_ruleset.json \
    --language sql \
    --schema data/demo_schema_singapore_citizenship.sql
```

To run your own document, swap in any PDF or image and set `--scenario` to describe what you want extracted. The `--corpus` flag accepts any annotation CSV in the same format as `data/annotation/annotations.csv` if you want automated recall metrics against your own ground truth.

---

## Data

**Source**: Four eligibility PDFs downloaded from official Singapore government websites — HDB (CPF Housing Grants), Ministry of Finance / CPF Board (GST Voucher), ICA (Singapore Citizenship), Ministry of Health / AIC (CHAS). All documents are publicly available

**Privacy**: None of the source documents contain personal data. This prototype processes only policy text. The generated code runs against applicant data on the user's own infrastructure — no personal data ever reaches the LLM.

**Annotation corpus**: 85 conditions hand-labelled across the four documents. Each condition was annotated with its verbatim source clause, condition type (from a seven-type taxonomy), AND/OR group logic, and escalation status. Annotation took roughly four hours. The corpus skews toward Singapore-specific eligibility structures (tiered household income ceilings, dual citizenship/PR pathways, HDB flat types) and will need extension before generalising to other domains.

**Demo schema**: `data/demo_schema_singapore_citizenship.sql` is a PostgreSQL schema I wrote for the Singapore Citizenship use case. It is synthetic — not derived from any actual ICA database — and is intended only to demonstrate the SQL generation capability.

---

## Limitations

- **Stage 2 code requires human review** before running against real data. The generated `evaluate()` function preserves the extracted logic, but operator direction errors (e.g. `>` vs `>=`) and wrong threshold values can slip through, especially on computation conditions where the formula is represented as a stub.
- **OR group accuracy is 62%** overall with the `direct` extraction variant. The model flattens tiered alternatives (income ceilings, grant amounts by citizenship/flat size) into AND lists roughly 40% of the time. The CoT-with-examples prompt addresses this partially by tagging each condition with its document structure (table row, bullet under "You must:", conditional alternative).
- **Computation conditions generate formula stubs**, not working formulas. Income averaging over months worked, lease-coverage formulas, and pro-rated grant amounts require the implementer to fill in the derivation logic. The stub includes the source clause and comparison operator so the task is well-defined.
- **The annotation corpus is small**. 85 conditions across 4 documents is enough to measure the main failure modes but not enough to be statistically confident in the numbers. More documents would tighten the estimates.
- **Infographic extraction** relies on Gemini's native vision capability. Source clause verification is skipped for images (OCR text and model-read text systematically diverge) — every extracted condition is flagged for visual verification.

---

## Deployment considerations

**Who runs it, where**: A small policy-tech or policy-officer team within any agency (e.g MOM, HDB), working with authorizing certain policy implementations (e.g Grants, Application for Work Passes). They will have access to agency policy documents and sensitive data, where the tool bridges the gap. In production, the Streamlit UI would be replaced by an API endpoint that accepts a document and returns a RuleSet JSON for downstream systems to generate code from.

**Inference cost** (Source: OpenRouter pricing): 
- A PDF page contains between 600-2000 tokens of text (average 1,300).
- Stage 1 with Gemini 3.5 Flash: $1.50/M tokens → $0.003 per page (average 1,300 tokens of extracted text + 1,500 tokens of prompt) → $0.03 per document (10 pages).
- Stage 2 with hybrid GPT-5.3 Codex: $1.75/M tokens → $0.00000175 per token → $0.01–$0.05 per ruleset depending on size and complexity.
- At 50 policy documents per month, total API cost is under $5/month. The bottleneck is latency (40–65 seconds per document), not cost.

**Monitoring**: Track hallucination rate per document (threshold is < 25%), OR group accuracy, and Stage 2 guardrail pass rate. A hallucination spike signals a document format pdfplumber handles poorly; a guardrail failure rate above 10% signals a prompt regression.

**Monitoring**: Track hallucination rate per document (target below 15% for clean PDFs), OR group accuracy, and Stage 2 guardrail pass rate. A hallucination spike signals a document format pdfplumber handles poorly; a guardrail failure rate above 10% signals a prompt regression.

**One risk**: A policy changes, e.g new income ceiling, adjusted eligibility criterion and the regenerated code silently produces different verdicts for boundary cases with no diff or alert. Anyone still running the old version gets wrong answers. Production needs versioned rulesets, change detection against previous extractions, and mandatory human sign-off before new code replaces old. Perhaps in the future, a database of existing code or documents can be incorporated to make this more robust.