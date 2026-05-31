"""Document-to-Criteria Checker — Streamlit web interface."""

from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

st.set_page_config(
    page_title="Document-to-Criteria Checker",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Global CSS — always-visible copy button, scrollable code blocks
st.markdown("""
<style>
/* Always show the copy button on code blocks */
.stCode button { opacity: 1 !important; visibility: visible !important; }
/* Metric cards */
.metric-box {
    background: #f8f9fa; border: 1px solid #e0e0e0;
    border-radius: 10px; padding: 14px 18px; text-align: center;
}
.metric-box .value { font-size: 1.8rem; font-weight: 700; color: #1a1a1a; }
.metric-box .label { font-size: 0.8rem; font-weight: 600; color: #555; margin-bottom: 2px; }
.metric-box .desc  { font-size: 0.72rem; color: #888; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

import app.config as _cfg

S1_MODEL   = _cfg.get("MODEL_STAGE1",         "google/gemini-3.5-flash")
S2_MODEL   = _cfg.get("MODEL_STAGE2",          "openai/gpt-5.4")
S1_VARIANT = _cfg.get("STAGE1_VARIANT",        "cot_examples")
S1_HINTS   = bool(_cfg.get("STAGE1_USE_HINTS", False))
S2_VARIANT = _cfg.get("STAGE2_VARIANT",        "hybrid")

# Images only — no GIFs
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("Document-to-Criteria Checker")
    st.divider()
    st.caption(f"**Stage 1** `{S1_VARIANT}` · `{S1_MODEL}`")
    st.caption(f"**Stage 2** `{S2_VARIANT}` · `{S2_MODEL}`")
    st.divider()
    st.caption("Edit `config.json` to change models and methods.")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _init() -> None:
    defaults = {
        "ruleset": None, "ruleset_json": "",
        "document_text": "", "is_image": False, "removed_ids": set(),
        "results_collapsed": False, "selected_language": "Python",
        # Per-language code cache — switching language loads from cache instantly
        "_cache_python": None,   # {"code", "guardrails", "ext", "lang"}
        "_cache_sql":    None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_upload(f) -> str:
    suffix = pathlib.Path(f.name).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(f.read())
        return tmp.name

def _is_image(name: str) -> bool:
    return pathlib.Path(name).suffix.lower() in _IMAGE_EXTS

def _load_ruleset(data: dict):
    from app.models.schemas import RuleSet as _RS
    from app.pipeline.extractor import _normalize_node
    if "root" in data:
        _normalize_node(data["root"])
    if "escalated_rules" in data:
        data["escalated_rules"] = [r for r in data["escalated_rules"] if isinstance(r, dict)]
        for r in data["escalated_rules"]:
            _normalize_node(r)
    return _RS.model_validate(data)

def _token_str(steps_or_comp) -> str:
    if isinstance(steps_or_comp, list):
        ti = sum(s.get("input_tokens", 0) for s in steps_or_comp)
        to = sum(s.get("output_tokens", 0) for s in steps_or_comp)
        ms = sum(s.get("latency_ms", 0) for s in steps_or_comp)
        return f"{ti}↑ {to}↓ · {ms:.0f}ms"
    return f"{steps_or_comp.input_tokens}↑ {steps_or_comp.output_tokens}↓ · {steps_or_comp.latency_ms:.0f}ms"

def _all_leaves(node) -> list:
    if node.is_leaf:
        return [node]
    out = []
    for c in node.conditions:
        out.extend(_all_leaves(c))
    return out

# ---------------------------------------------------------------------------
# Code rendering helpers
# ---------------------------------------------------------------------------

def _render_code_scrollable(code: str, language: str, height: int = 380) -> None:
    """Render a syntax-highlighted scrollable code block using Pygments HTML."""
    try:
        from pygments import highlight
        from pygments.lexers import PythonLexer, SqlLexer
        from pygments.formatters import HtmlFormatter
        lexer = PythonLexer() if language == "python" else SqlLexer()
        # noclasses=True embeds all styles inline — no external CSS needed
        formatter = HtmlFormatter(style="monokai", noclasses=True, nowrap=False)
        highlighted = highlight(code, lexer, formatter)
        st.markdown(
            f'<div style="max-height:{height}px;overflow-y:auto;border-radius:6px;'
            f'border:1px solid #e0e0e0">{highlighted}</div>',
            unsafe_allow_html=True,
        )
    except Exception:
        st.code(code, language=language)


def _find_sql_fragment(sql: str, node) -> str | None:
    """Find the SQL fragment relevant to a condition node.

    Searches for:
      1. The rule_id in a comment (-- rule_id:)
      2. The field_required name as a column reference
      3. A line containing the condition description keywords
    """
    field = getattr(node, "field_required", None) or ""
    rule_id = getattr(node, "rule_id", "") or ""

    lines = sql.splitlines()
    # Find the block of lines around the field or rule_id mention
    target_lines: list[int] = []
    for i, line in enumerate(lines):
        if (field and field.lower() in line.lower()) or \
           (rule_id and rule_id.lower() in line.lower()):
            target_lines.append(i)

    if not target_lines:
        return None

    # Return a window of context around the first match
    start = max(0, target_lines[0] - 1)
    end   = min(len(lines), target_lines[-1] + 3)
    fragment = "\n".join(lines[start:end]).strip()
    return fragment if fragment else None


# ---------------------------------------------------------------------------
# Condition tree renderer
# ---------------------------------------------------------------------------

_TYPE_COLOUR = {
    "threshold": "#1976D2", "membership": "#388E3C",
    "more_info_needed": "#388E3C", "temporal": "#F57C00",
    "computation": "#7B1FA2", "sequential": "#0097A7",
    "existence": "#5D4037", "discretionary": "#C62828",
}
_TYPE_LABEL = {"more_info_needed": "membership"}
_LOGIC_COLOUR = {"AND": "#455A64", "OR": "#E65100"}


def _node_html(node, depth: int = 0, removed: set = set()) -> str:
    if node.rule_id in removed:
        return ""
    indent = depth * 22
    if node.is_leaf:
        ctype = node.condition_type.value if node.condition_type else "?"
        display_ctype = _TYPE_LABEL.get(ctype, ctype)
        colour = _TYPE_COLOUR.get(ctype, "#78909C")
        badge = (
            f'<span style="background:{colour};color:#fff;padding:1px 6px;'
            f'border-radius:3px;font-size:0.72rem;font-weight:600">{display_ctype}</span>'
        )
        # Flags: yellow ⚠ for discretionary, red ⚠ for unverified
        flags = ""
        if ctype == "discretionary":
            flags = ' <span style="color:#F57F17;font-size:0.8rem" title="Requires human judgment">⚠</span>'
        elif getattr(node, "hallucination_risk", False):
            flags = ' <span style="color:#C62828;font-size:0.8rem" title="Source clause unverified — please check">⚠</span>'
        source = node.source_clause[:120].replace("<", "&lt;").replace(">", "&gt;")
        return (
            f'<div style="margin-left:{indent}px;margin-bottom:6px;'
            f'border-left:3px solid {colour};padding-left:8px">'
            f'{badge}{flags} '
            f'<span style="font-size:0.88rem">{node.description[:100]}</span>'
            f'<div style="font-size:0.75rem;color:#666;margin-top:2px">📄 {source}</div>'
            f'</div>'
        )
    else:
        logic = node.logic or "AND"
        lcolour = _LOGIC_COLOUR.get(logic, "#455A64")
        label = (
            f'<span style="background:{lcolour};color:#fff;padding:1px 7px;'
            f'border-radius:3px;font-size:0.72rem;font-weight:700">{logic}</span>'
            f' <span style="font-size:0.82rem;color:#555">{node.description[:80]}</span>'
        )
        children_html = "".join(_node_html(c, depth + 1, removed) for c in node.conditions)
        return (
            f'<div style="margin-left:{indent}px;margin-bottom:4px">{label}</div>'
            f'{children_html}'
        )


def _tree_html_standalone(ruleset) -> str:
    """Full standalone HTML document for tree download."""
    removed = st.session_state.get("removed_ids", set())
    body = _node_html(ruleset.root, removed=removed)
    for rule in ruleset.escalated_rules:
        body += _node_html(rule, removed=removed)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{ruleset.policy_name} — Criteria Tree</title>
<style>body{{font-family:sans-serif;padding:24px;line-height:1.6}}
.legend{{margin-bottom:16px;font-size:0.8rem;color:#555}}
.legend span{{display:inline-block;margin-right:12px}}</style>
</head><body>
<h2>{ruleset.policy_name}</h2>
<p style="color:#555">{ruleset.constraint_scenario}</p>
<div class="legend">
<span>⚠ yellow = discretionary (human judgment)</span>
<span>⚠ red = unverified source clause</span>
</div>
{body}
</body></html>"""


def _render_tree(ruleset) -> None:
    removed = st.session_state.get("removed_ids", set())
    html = _node_html(ruleset.root, removed=removed)

    st.markdown(
        f'<div style="font-family:sans-serif;line-height:1.6;border:1px solid #e0e0e0;'
        f'border-radius:6px;padding:12px;max-height:420px;overflow-y:auto">{html}</div>',
        unsafe_allow_html=True,
    )

    # Download tree as HTML
    tree_html = _tree_html_standalone(ruleset)
    st.download_button(
        "⬇️ Download criteria tree (HTML)",
        data=tree_html.encode("utf-8"),
        file_name="criteria_tree.html",
        mime="text/html",
        use_container_width=True,
    )

    # Escalated conditions
    if ruleset.escalated_rules:
        st.divider()
        st.caption(f"⚠ **{len(ruleset.escalated_rules)} discretionary** — require officer judgment")
        for rule in ruleset.escalated_rules:
            ctype = rule.condition_type.value if rule.condition_type else "?"
            note = rule.escalation_note or "officer judgment required"
            st.warning(
                f"**{rule.rule_id}** (`{ctype}`) — {rule.description}\n\n"
                f"> {rule.source_clause[:200]}\n\n_{note}_"
            )

    # Remove conditions — scrollable fixed-height box
    leaves = _all_leaves(ruleset.root)
    visible = [l for l in leaves if l.rule_id not in removed]
    if visible:
        st.divider()
        st.caption("**Remove conditions** — tick any to exclude from code generation")
        to_remove = set()
        with st.container(height=200, border=True):
            for leaf in visible:
                ctype = leaf.condition_type.value if leaf.condition_type else "?"
                if st.checkbox(
                    f"`{ctype}` {leaf.description[:70]}",
                    key=f"rm_{leaf.rule_id}",
                ):
                    to_remove.add(leaf.rule_id)
        if to_remove:
            if st.button("Apply removals", type="primary"):
                st.session_state["removed_ids"] |= to_remove
                st.rerun()

# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

col_left, col_right = st.columns([1, 1], gap="large")

# ============================================================
# LEFT COLUMN
# ============================================================

with col_left:
    st.title("Document-to-Criteria Checker")
    st.caption(
        "Document-to-Criteria Checker is a tool that allows users to upload a document "
        "to extract criterias and conditions present in the document e.g eligibilities, "
        "requirements, etc. Especially for data sensitivity use cases, one shouldn't "
        "utilise LLMs (unless fully local with guardrails) for authorisation requests by "
        "sending personal information to it. This tool therefore utilises LLMs to extract "
        "criterias, context and conditions from documents and develop a deterministic "
        "ruleset for users to apply to their databases for faster, effective authorisation "
        "requests without compromising data privacy."
    )
    st.divider()

    uploaded = st.file_uploader(
        "Upload a document (policy documents etc.)",
        type=["pdf", "png", "jpg", "jpeg", "webp"],
        label_visibility="visible",
        help="PDF or image (infographic). PDFs use pdfplumber; images use multimodal extraction.",
    )

    scenario = st.text_area(
        "What would you like to check on?",
        value="What are the criterias for this entitlement?",
        height=72,
        placeholder="E.g. Who is eligible for the grant? What are the requirements for the grant?",
    )

    if uploaded:
        fname = uploaded.name
        is_img_input = _is_image(fname)
        st.caption(
            f"📄 `{fname}` · "
            + ("🖼️ image — multimodal extraction" if is_img_input else "📝 PDF — text extraction")
        )

    run_btn = st.button(
        "▶ Extract Criteria",
        type="primary",
        disabled=uploaded is None,
        use_container_width=True,
    )

    if run_btn and uploaded:
        tmp_path = _save_upload(uploaded)
        is_img_input = _is_image(uploaded.name)
        st.session_state["is_image"] = is_img_input
        st.session_state["removed_ids"] = set()
        st.session_state["results_collapsed"] = False

        progress = st.progress(0, text="Starting…")
        try:
            from app.pipeline.extractor import extract_rules
            from app.pipeline.pdf_parser import extract_chunks

            progress.progress(10, text="📂 Parsing document…")
            if not is_img_input:
                chunks = extract_chunks(tmp_path)
                st.session_state["document_text"] = "\n\n".join(c.text for c in chunks)
            else:
                st.session_state["document_text"] = ""

            progress.progress(30, text=f"🤖 Running extraction…")
            ruleset, comps = extract_rules(
                pdf_path=tmp_path,
                constraint_scenario=scenario,
                variant=S1_VARIANT,
                model=S1_MODEL,
                use_hints=S1_HINTS,
            )

            progress.progress(80, text="🛡️ Applying guardrails…")
            if is_img_input and not st.session_state["document_text"]:
                try:
                    from app.utils.llm_client import ocr_image
                    st.session_state["document_text"] = ocr_image(tmp_path)
                except Exception:
                    pass

            st.session_state["ruleset"] = ruleset
            st.session_state["ruleset_json"] = ruleset.model_dump_json(indent=2)
            st.session_state["generated_code"] = ""
            st.session_state["guardrail_summary"] = None
            progress.progress(100, text=f"✅ Done · {_token_str(comps)}")

        except Exception as exc:
            progress.empty()
            st.error(f"Extraction failed: {exc}")
            raise

    if st.session_state.get("is_image") and st.session_state.get("ruleset"):
        st.caption("🖼️ Image input — verify conditions visually against the source document.")

# ============================================================
# RIGHT COLUMN
# ============================================================

with col_right:
    ruleset = st.session_state.get("ruleset")
    has_ruleset = bool(ruleset is not None and st.session_state.get("ruleset_json"))
    results_expanded = has_ruleset and not st.session_state.get("results_collapsed", False)

    # ── Extraction Results ────────────────────────────────────────────────────
    n_conditions = len(_all_leaves(ruleset.root)) if has_ruleset else 0
    with st.expander(
        f"Extraction Results{f' — {n_conditions} criteria found' if has_ruleset else ''}",
        expanded=bool(results_expanded),
    ):
        if not has_ruleset:
            st.caption("Run extraction on the left to see results here.")
        else:
            removed = st.session_state.get("removed_ids", set())
            all_leaves_list = _all_leaves(ruleset.root)
            visible_count = len([l for l in all_leaves_list if l.rule_id not in removed])
            hall_count = ruleset.hallucination_risk_count

            # Centralised metrics in rounded boxes
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(
                    f'<div class="metric-box">'
                    f'<div class="label">Criterias</div>'
                    f'<div class="value">{visible_count}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with c2:
                st.markdown(
                    f'<div class="metric-box">'
                    f'<div class="label">⚠ Discretionary</div>'
                    f'<div class="value">{len(ruleset.escalated_rules)}</div>'
                    f'<div class="desc">Criterias with some subjective interpretation, requires human judgement</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with c3:
                st.markdown(
                    f'<div class="metric-box">'
                    f'<div class="label">🔴 To check</div>'
                    f'<div class="value">{hall_count}</div>'
                    f'<div class="desc">Sometimes LLMs may overexaggerate beyond our confidence threshold, do check these flagged criterias!</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            st.divider()
            _render_tree(ruleset)
            st.divider()

            st.download_button(
                "⬇️ Download RuleSet JSON",
                data=st.session_state["ruleset_json"],
                file_name="ruleset.json",
                mime="application/json",
                use_container_width=True,
            )

            # Collapse button at the bottom
            if st.button("↑ Collapse results", use_container_width=True):
                st.session_state["results_collapsed"] = True
                st.rerun()

    # ── Code Generation ───────────────────────────────────────────────────────
    cache_py  = st.session_state.get("_cache_python")
    cache_sql = st.session_state.get("_cache_sql")
    has_any_code = bool(cache_py or cache_sql)

    with st.expander(
        "Code Generation" + (" ✓" if has_any_code else ""),
        expanded=has_any_code,
    ):
        if not has_ruleset:
            st.caption("Extract criteria first.")
        else:
            # Language selector
            lang_choice = st.radio(
                "Output language",
                ["Python", "SQL"],
                horizontal=True,
                index=0 if st.session_state.get("selected_language", "Python") == "Python" else 1,
            )
            st.session_state["selected_language"] = lang_choice
            is_sql = lang_choice == "SQL"
            cache_key = "_cache_sql" if is_sql else "_cache_python"
            cached = st.session_state.get(cache_key)

            # Schema input — available for both Python and SQL
            # For SQL: upload the CREATE TABLE DDL or describe the table columns
            # so the generator maps extracted field names to your actual column names.
            schema_help = (
                "Upload a SQL DDL file (.sql) or describe your table columns so field names "
                "are mapped to your schema. e.g. applicant_id, age INTEGER, citizenship VARCHAR(10)"
                if is_sql else
                "JSON Schema, TypedDict, SQL DDL, YAML, or TypeScript interface. "
                "Maps extracted field names to your system's field names."
            )
            schema_placeholder = (
                "e.g. age INTEGER, citizenship VARCHAR(10), pr_grant_date DATE\n"
                "Or paste your CREATE TABLE statement here."
                if is_sql else
                "e.g. age: int, citizenship: str (SC/SPR), monthly_income: float"
            )

            schema_file = st.file_uploader(
                "Upload schema file (optional)",
                type=["json", "py", "sql", "yaml", "yml", "ts", "tsx"],
                key=f"schema_file_upload_{lang_choice}",
                help=schema_help,
            )
            user_schema_text = st.text_area(
                "Or describe schema in text (optional)",
                height=68,
                placeholder=schema_placeholder,
            )

            schema_warning = ""
            if schema_file is not None:
                from app.stage2.schema_interpreter import parse_schema_file
                parsed_schema, schema_warning = parse_schema_file(
                    schema_file.name, schema_file.read()
                )
                st.caption(f"Parsed `{schema_file.name}` — {len(parsed_schema.splitlines())} fields")
                st.code(parsed_schema[:400] + ("…" if len(parsed_schema) > 400 else ""), language="text")
                if schema_warning:
                    st.warning(schema_warning)
                user_schema = parsed_schema
            else:
                user_schema = user_schema_text

            # Button label changes if cache exists for this language
            btn_label = f"↻ Regenerate {lang_choice}" if cached else f"▶ Generate {lang_choice}"
            if st.button(btn_label, type="primary", use_container_width=True):
                schema_input = user_schema.strip() or None
                gen_progress = st.progress(0, text=f"Generating {lang_choice}…")
                try:
                    from app.stage2 import generator_deterministic, generator_llm, generator_hybrid
                    from app.stage2.generator_sql import generate as gen_sql
                    from app.stage2.guardrails import apply_guardrails
                    from app.stage2.guardrails_sql import apply_sql_guardrails

                    gen_progress.progress(20, text=f"🏗️ Building {lang_choice} output…")

                    if is_sql:
                        code = gen_sql(ruleset)
                        # Apply schema field mapping to SQL if a schema was provided
                        if schema_input:
                            from app.stage2.schema_interpreter import (
                                interpret as interp_schema,
                                apply_mapping as apply_sql_mapping,
                            )
                            from app.stage2.generator_deterministic import _collect_leaves as _sql_leaves
                            field_names = list({
                                n.field_required for n in _sql_leaves(ruleset.root)
                                if n.field_required
                            })
                            mapping = interp_schema(field_names, schema_input, model=S2_MODEL)
                            if any(k != v for k, v in mapping.items()):
                                # For SQL, substitute column name references directly
                                import re as _re
                                for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
                                    if old != new:
                                        code = _re.sub(rf'\b{_re.escape(old)}\b', new, code)
                        file_ext, code_lang = ".sql", "sql"
                        guard = apply_sql_guardrails(code, ruleset)
                    elif S2_VARIANT == "deterministic":
                        code = generator_deterministic.generate(ruleset)
                        file_ext, code_lang = ".py", "python"
                        guard = apply_guardrails(code, ruleset)
                    elif S2_VARIANT == "llm":
                        code, _ = generator_llm.generate(ruleset, model=S2_MODEL)
                        file_ext, code_lang = ".py", "python"
                        guard = apply_guardrails(code, ruleset)
                    else:  # hybrid
                        res = generator_hybrid.generate(ruleset, user_schema=schema_input, model=S2_MODEL)
                        code = res.reviewed_code
                        file_ext, code_lang = ".py", "python"
                        guard = apply_guardrails(code, ruleset)

                    gen_progress.progress(90, text="🛡️ Guardrails…")
                    st.session_state[cache_key] = {
                        "code": code, "guardrails": guard.summary(),
                        "ext": file_ext, "lang": code_lang,
                    }
                    gen_progress.progress(100, text="✅ Done")
                    cached = st.session_state[cache_key]

                except Exception as exc:
                    gen_progress.empty()
                    st.error(f"Code generation failed: {exc}")
                    raise

            # Show cached output for the selected language
            if cached:
                code      = cached["code"]
                gs        = cached["guardrails"]
                code_lang = cached["lang"]
                file_ext  = cached["ext"]

                if gs.get("passed"):
                    st.success("Guardrails passed ✓")
                else:
                    st.warning("Some guardrails failed — review before use")
                    failed = [k for k, v in gs.items() if k != "passed" and v is False]
                    st.caption("Failed: " + ", ".join(failed))

                st.divider()
                st.caption(f"**Generated {code_lang.upper()} code**")
                _render_code_scrollable(code, code_lang, height=380)

                # Source clause ↔ generated fragment
                st.divider()
                st.caption("**Source clause ↔ Generated output**")
                all_lv = _all_leaves(ruleset.root)
                for rule in ruleset.escalated_rules:
                    all_lv.extend(_all_leaves(rule))
                labels = {l.rule_id: l.description[:60] for l in all_lv}
                sel_id = st.selectbox(
                    "Condition", list(labels.keys()),
                    format_func=lambda x: labels.get(x, x),
                    key=f"sel_condition_{code_lang}",
                )
                sel = next((l for l in all_lv if l.rule_id == sel_id), None)
                if sel:
                    st.info(f'📄 "{sel.source_clause}"')
                    if code_lang == "sql":
                        fragment = _find_sql_fragment(code, sel)
                        if fragment:
                            st.code(fragment, language="sql")
                        else:
                            st.caption("Fragment not found — condition may be a CTE stub or comment.")
                    else:
                        slug = re.sub(r"[^a-zA-Z0-9]", "_", sel.rule_id).lower().strip("_")
                        for prefix in ("check", "escalate"):
                            m = re.search(
                                rf"(def {prefix}_{re.escape(slug)}\b.*?)(?=\ndef |\Z)",
                                code, re.DOTALL,
                            )
                            if m:
                                st.code(m.group(1).strip(), language="python")
                                break
                        else:
                            st.caption(f"Function not found for `{sel.rule_id}`.")

                st.divider()
                _mime = "text/x-sql" if file_ext == ".sql" else "text/x-python"
                st.download_button(
                    f"⬇️ Download {'SQL query' if file_ext == '.sql' else 'Python code'}",
                    data=code,
                    file_name=f"generated_policy{file_ext}",
                    mime=_mime,
                    use_container_width=True,
                )

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()
st.caption(
    "Document-to-Criteria Checker · All generated code requires human review before production use. "
    "Discretionary conditions require officer judgment."
)
