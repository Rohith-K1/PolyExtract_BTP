"""polyextract — human-in-the-loop review, data-quality dashboard and the three application workflows.

Run:  .venv/Scripts/python.exe -m streamlit run streamlit_app.py
"""

import json
import sqlite3

import pandas as pd
import streamlit as st

from polyextract.apps.workflows import answer_with_evidence, forward_predict, retrieve
from polyextract.config import get_settings
from polyextract.dataset.builder import quality_stats, record_rows
from polyextract.review.policy import audit_queue, audit_status
from polyextract.review.service import (
    Decision,
    ReviewError,
    decide,
    decide_many,
    group_rows,
    history,
    load_record,
    queue,
    source_table,
    table_groups,
)
from polyextract.schemas.molecular import DESCRIPTOR_FIELDS
from polyextract.storage.records import init_schema

st.set_page_config(page_title="polyextract", page_icon=":material/science:", layout="wide")
DB = get_settings().resolved_data_dir() / "db" / "polyextract.sqlite"


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, check_same_thread=False, timeout=60)  # extraction may be writing concurrently
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


@st.cache_data(ttl=30, max_entries=4)
def verified_df() -> pd.DataFrame:
    with conn() as c:
        return pd.DataFrame(record_rows(c))


st.title(":material/science: polyextract")
st.caption("Evidence-linked polymer data from published literature. Every value links back to its DOI and source passage.")
view = st.segmented_control(
    "View", ["Review", "Dashboard", "Download", "Predict", "Retrieve", "Ask"], default="Review",
    label_visibility="collapsed",
)

# --------------------------------------------------------------------------------------- review
if view == "Review":
    c = conn()
    with st.sidebar:
        reviewer = st.text_input("Reviewer name", key="reviewer")
        mode = st.segmented_control("Mode", ["By table", "By record", "Audit"], default="By table",
                                    help="By table: check every extracted row against the original table, then decide "
                                         "for all of them. Audit: random sample of machine-verified records — your "
                                         "verdicts measure the machine tier's precision. Each record keeps its own audit entry.")
        if mode == "By record":
            status = st.segmented_control("Status", ["pending", "accepted", "corrected", "rejected", "needs_reextraction"],
                                          default="pending")
            route = st.selectbox("Route", ["all", "review_low_risk", "needs_review", "blocked_errors"])
        from polyextract.review.dedup import dedup_counts

        hidden = dedup_counts(c)
        if hidden:
            st.caption(f":material/filter_alt: Hidden from review automatically: {hidden.get('duplicate', 0)} duplicate(s), "
                       f"{hidden.get('excluded_computed', 0)} computed value(s). Each is logged in its audit trail.")

    if mode == "By table":
        groups = table_groups(c)
        n_pending = sum(len(g["record_ids"]) for g in groups)
        st.subheader(f"{len(groups)} source table(s) · {n_pending} pending record(s)")
        if not groups:
            st.info("No pending records. Run `polyextract extract` to create candidates.", icon=":material/inbox:")
            st.stop()
        titles = {r["paper_id"]: json.loads(r["payload"]).get("title") or r["paper_id"]
                  for r in c.execute("SELECT paper_id, payload FROM papers WHERE paper_id IN (%s)"
                                     % ",".join("?" * len({g["paper_id"] for g in groups})),
                                     list({g["paper_id"] for g in groups}))}
        gi = st.selectbox("Source", range(len(groups)), format_func=lambda i: (
            f"{len(groups[i]['record_ids'])} rec · {groups[i]['source']} · "
            f"{', '.join(f'{k}:{v}' for k, v in groups[i]['routes'].items())} · {titles.get(groups[i]['paper_id'], '')[:80]}"))
        g = groups[gi]
        paper = load_record(c, g["record_ids"][0]).paper
        st.markdown(f"**{paper.title}**  \n{paper.journal or ''} {paper.publication_year or ''} · "
                    f"[doi:{paper.doi}](https://doi.org/{paper.doi})")
        src = source_table(c, g["paper_id"], g["source"], get_settings().resolved_data_dir() / "raw" / "fulltext" / "epmc")
        with st.container(border=True):
            st.markdown("**1. Original source** (from the open-access full text)")
            if src:
                st.caption(f"{src['label']}: {src['caption']}")
                hdr = [h or f"col{j}" for j, h in enumerate(src["headers"])]
                hdr = [h if hdr.count(h) == 1 else f"{h} [{j}]" for j, h in enumerate(hdr)]
                st.dataframe(pd.DataFrame([r + [""] * (len(hdr) - len(r)) for r in src["rows"]], columns=hdr),
                             hide_index=True, alt="Original table from the paper")
                for fn in src["footnotes"]:
                    st.caption(fn)
            else:
                st.caption("Text passage(s): verbatim quotes are shown per record below.")
                for rid in g["record_ids"][:50]:
                    ev = load_record(c, rid).measurement.value.evidence
                    if ev:
                        st.code(ev[0].source_text, language=None, wrap_lines=True)
        rows = group_rows(c, g["record_ids"])
        with st.container(border=True):
            st.markdown("**2. What was extracted** — tick the rows that match the source exactly")
            df = pd.DataFrame(rows)
            # never pre-ticked by the app: "Tick all" is the reviewer's own deliberate action after comparing tables
            fill = st.segmented_control("Quick fill", ["Tick all ✓", "Tick all ✗"], key=f"fill_{gi}_{g['paper_id']}",
                                        help="Fills every row; then untick the exceptions. Choose again to clear.")
            df.insert(0, "ok", fill == "Tick all ✓")
            df.insert(1, "wrong", fill == "Tick all ✗")
            edited = st.data_editor(
                df, hide_index=True, key=f"ed_{gi}_{g['paper_id']}_{fill}",
                disabled=[col for col in df.columns if col not in ("ok", "wrong")],
                column_config={"ok": st.column_config.CheckboxColumn("✓", help="Matches the paper"),
                               "wrong": st.column_config.CheckboxColumn("✗", help="Wrong / not in the paper"),
                               "record_id": None,
                               "errors": st.column_config.TextColumn("Errors (blocks accept)"),
                               "confidence": st.column_config.ProgressColumn("Conf.", min_value=0, max_value=1, format="%.2f")})
        both = edited["ok"] & edited["wrong"]
        picked = edited.loc[edited["ok"] & ~both, "record_id"].tolist()
        wrong = edited.loc[edited["wrong"] & ~both, "record_id"].tolist()
        if both.any():
            st.warning(f"{int(both.sum())} row(s) are ticked both ✓ and ✗ and will be left pending.")
        with st.form("bulk"):
            comment = st.text_input("Comment (optional, e.g. what was wrong)")
            b1, b2, b3 = st.columns(3)
            acc = b1.form_submit_button(f"Accept {len(picked)} ✓ rows", icon=":material/check:", type="primary", width="stretch")
            rej = b2.form_submit_button(f"Reject {len(wrong)} ✗ rows", icon=":material/close:", width="stretch")
            rex = b3.form_submit_button(f"Re-extract {len(wrong)} ✗ rows", icon=":material/refresh:", width="stretch")
        action = (Decision.ACCEPT, picked) if acc else (Decision.REJECT, wrong) if rej else \
            (Decision.REQUEST_REEXTRACTION, wrong) if rex else None
        if action and not action[1]:
            st.info("No rows ticked for that action.")
            action = None
        if action:
            try:
                out = decide_many(c, action[1], action[0], reviewer, comment)
                st.cache_data.clear()
                st.success(f"{action[0].value}: {len(out['applied'])} record(s) saved", icon=":material/task_alt:")
                if out["skipped"]:
                    st.warning(f"{len(out['skipped'])} skipped (fix one by one in 'By record'):\n\n"
                               + "\n".join(f"- {v}" for v in list(out["skipped"].values())[:5]))
                if st.button("Next table", icon=":material/arrow_forward:"):
                    st.rerun()
            except ReviewError as e:
                st.error(str(e), title="Not saved")
        st.caption("Rows with errors cannot be accepted here. Open them in **By record** to correct or reject.")
        st.stop()

    if mode == "Audit":
        a = audit_status(c)
        k = st.container(horizontal=True)
        k.metric("Machine-verified pool", a["machine_verified_records"], border=True)
        k.metric("Audited", f"{a['audit_done']} / {a['audit_drawn']}", border=True)
        k.metric("Precision", f"{a['precision']:.1%}" if a["precision"] is not None else "—", border=True,
                 help=f"Wilson 95% CI {a['precision_ci95']}" if a["precision_ci95"] else None)
        k.metric("Tier released", "yes" if a["released"] else "no", border=True, help=a["release_rule"])
        st.caption("Judge each record only against the source evidence: **accept** if exactly right, otherwise "
                   "**correct** or **reject**. Your verdicts are the precision estimate — do not skip hard ones.")
        rows = audit_queue(c)
        st.subheader(f"Audit queue: {len(rows)} record(s)")
        if not rows:
            st.info("No audit records waiting. Draw a sample with `polyextract verify --draw-audit 300`.",
                    icon=":material/casino:")
            st.stop()
    else:
        find = st.text_input("Find record (sample name or DOI part, e.g. N5 or langmuir)", key="find_record")
        rows = queue(c, status or "pending", None if route == "all" else route, limit=5000)
        if find.strip():
            rows = [r for r in rows if find.strip().casefold() in r["record_id"].casefold()]
        st.subheader(f"Queue: {len(rows)} record(s)")
    if not rows:
        st.info("Nothing in this queue. Run `polyextract extract` to create candidate records.", icon=":material/inbox:")
        st.stop()
    labels = {r["record_id"]: f"[{r['route']}] {r['confidence']:.2f} · {r['property_name']} · {r['sample_id'].split('#s:')[-1]} · {r['paper_id']}" for r in rows}
    rid = st.selectbox("Record", list(labels), format_func=labels.get)
    v = load_record(c, rid)
    m, mc, s, p = v.measurement, v.molecular, v.sample, v.paper

    left, right = st.columns([3, 2])
    with left:
        with st.container(border=True):
            st.markdown(f"**{p.title}**  \n{p.journal or ''} {p.publication_year or ''} · [doi:{p.doi}](https://doi.org/{p.doi})")
            st.markdown(f"Sample **{s.sample_label}**: {s.polymer_name} · amorphous status: `{s.amorphous_status.value}`")
        with st.container(border=True):
            st.markdown("**Property measurement**")
            st.markdown(f"{m.property_name_reported} → `{m.property_name.value}` = **{m.value.reported_text}** {m.value.unit or ''}"
                        f" → {m.value.normalized_value} {m.value.normalized_unit or ''} · technique: {m.technique or 'n/a'}")
            if mc:
                st.markdown("**Molecular characteristics** (technique: `%s`)" % mc.technique.value)
                st.dataframe(pd.DataFrame([{"descriptor": f, "reported": getattr(mc, f).reported_text, "unit": getattr(mc, f).unit,
                                            "normalized": getattr(mc, f).normalized_value, "origin": getattr(mc, f).value_origin.value}
                                           for f in DESCRIPTOR_FIELDS if getattr(mc, f) is not None]),
                             hide_index=True, alt="Molecular descriptors for this sample")
        with st.container(border=True):
            st.markdown("**Source evidence** (verbatim)")
            for e in m.value.evidence:
                st.caption(f"{e.location_type.value} · {e.table_id or e.section or ''} · page {e.page or 'n/a'}")
                st.code(e.source_text, language=None, wrap_lines=True)
    with right:
        with st.container(border=True):
            st.metric("Confidence", f"{v.confidence:.2f}")
            st.bar_chart(pd.Series(v.components, name="score"), horizontal=True, height=260)
        with st.container(border=True):
            st.markdown("**Validation findings**")
            if v.findings:
                st.dataframe(pd.DataFrame(v.findings)[["severity", "category", "code", "message"]], hide_index=True,
                             alt="Validation findings for this record")
            else:
                st.success("No findings", icon=":material/check_circle:")

        with st.form("decision"):
            comment = st.text_area("Comment")
            corr = st.text_area("Corrections (JSON, for 'correct')",
                                placeholder='{"measurement": {"value": {...}}, "sample": {"polymer_name": "..."}}')
            choice = st.segmented_control("Decision", [d.value for d in Decision], default=Decision.ACCEPT.value)
            if st.form_submit_button("Submit decision", icon=":material/gavel:", type="primary", width="stretch"):
                try:
                    corrections = json.loads(corr) if corr.strip() else None
                    new = decide(c, rid, Decision(choice), reviewer, comment, corrections)
                    st.cache_data.clear()
                    st.success(f"Saved: {new}")
                except (ReviewError, json.JSONDecodeError, ValueError) as e:
                    st.error(str(e), title="Not saved")
        with st.expander("Audit trail"):
            st.dataframe(pd.DataFrame(history(c, rid)), hide_index=True, alt="Review history")

# ------------------------------------------------------------------------------------ dashboard
elif view == "Dashboard":
    with conn() as c:
        stats = quality_stats(c)
    k = st.container(horizontal=True)
    for label, key in [("Papers discovered", "papers_discovered"), ("Passing filters", "papers_passing_filters"),
                       ("Full texts processed", "papers_processed"), ("Candidate records", "candidate_records"),
                       ("Verified records", "verified_records"), ("Human-reviewed", "verified_human_reviewed"),
                       ("Machine-verified pool", "machine_verified_pool"), ("Pending review", "pending_review"),
                       ("Rejected", "rejected")]:
        k.metric(label, stats.get(key, 0), border=True)
    au = stats.get("audit", {})
    if au.get("audit_drawn"):
        st.caption(f"Audit ({au['policy']}): {au['audit_done']} of {au['audit_drawn']} sampled records reviewed · "
                   f"precision {au['precision']} (95% CI {au['precision_ci95']}) · machine tier released: "
                   f"**{au['released']}** ({au['release_rule']})")
    else:
        st.caption("Machine-verified records are not counted as verified until a random human audit passes "
                   "(`polyextract verify --draw-audit 300`, then Review → Audit).")
    a, b = st.columns(2)
    with a:
        st.markdown("**Descriptor availability (candidate records)**")
        st.bar_chart(pd.Series({f: v["n"] for f, v in stats["descriptor_availability"].items()}, name="records"))
        st.markdown("**Full-text acquisition**")
        st.bar_chart(pd.Series(stats.get("fulltext_status", {}), name="papers"))
    with b:
        st.markdown("**Property distribution (candidates)**")
        st.bar_chart(pd.Series(stats.get("property_distribution_candidates", {}), name="records"))
        st.markdown("**Validation failures**")
        st.dataframe(pd.Series(stats.get("validation_failures", {}), name="count").rename_axis("category|code").reset_index(),
                     hide_index=True, alt="Validation failure counts")
    with st.expander("All statistics (JSON)"):
        st.json(stats)

# ------------------------------------------------------------------------------------- download
elif view == "Download":
    st.markdown("Builds the dataset from what is in the database **right now**: your accepted and corrected records, "
                "plus the machine-verified records (labelled with their audit status). Duplicates and computed values "
                "are removed first. `DATASHEET.md` inside the zip gives every count.")
    version = st.text_input("Release version", value="v0.1")
    if st.button("Build dataset", type="primary", icon=":material/package_2:"):
        from polyextract.dataset.release import build_release, zip_release
        from polyextract.review.dedup import apply_dedup

        out_dir = get_settings().resolved_data_dir() / "processed" / f"submission_{version}"
        with st.spinner("Building…"):
            with conn() as c:
                apply_dedup(c)
                rel = build_release(c, out_dir, version)
            st.session_state["release"] = (version, rel, zip_release(out_dir))
    if "release" in st.session_state:
        version, rel, data = st.session_state["release"]
        k = st.container(horizontal=True)
        k.metric("Records", rel["dataset_records"], border=True)
        k.metric("Polymer samples", rel["dataset_samples"], border=True)
        k.metric("Papers", rel["dataset_papers"], border=True)
        k.metric("Human-reviewed", rel["dataset_tiers"].get("human_reviewed", 0), border=True)
        au = rel["audit"]
        k.metric("Audit", f"{au['audit_done']}/{au['audit_drawn']}", border=True)
        st.download_button(f"Download polyextract_{version}.zip", data, file_name=f"polyextract_{version}.zip",
                           mime="application/zip", icon=":material/download:")

# ----------------------------------------------------------------------------------- workflows
else:
    df = verified_df()
    if df.empty:
        st.warning("The application workflows use verified records only, and there are none yet. Review records first.",
                   icon=":material/lock:")
        st.stop()
    props = sorted(df["property_name"].unique())
    if view == "Predict":
        with st.form("predict"):
            prop = st.selectbox("Property", props)
            fam = st.selectbox("Polymer family", [None] + sorted(df["polymer_family"].dropna().unique()))
            c1, c2, c3 = st.columns(3)
            mn = c1.number_input("Mn (g/mol)", value=None, min_value=0.0)
            mw = c2.number_input("Mw (g/mol)", value=None, min_value=0.0)
            disp = c3.number_input("Đ", value=None, min_value=1.0)
            go = st.form_submit_button("Predict", icon=":material/trending_up:", type="primary")
        if go:
            pr = forward_predict(df, prop, polymer_family=fam, mn=mn, mw=mw, dispersity=disp)
            st.warning(pr.label, icon=":material/info:")
            if pr.predicted_value is not None:
                st.metric(f"Predicted {prop}", f"{pr.predicted_value:.4g} {pr.unit}")
            st.json({"model": pr.model, "training_records": pr.n_training_records, "training_papers": pr.n_training_papers,
                     "cv_metrics": pr.cv_metrics, "features": pr.features_used, "limitations": pr.limitations})
    elif view == "Retrieve":
        prop = st.selectbox("Property", props)
        sub = df[df["property_name"] == prop]
        lo, hi = float(sub["value"].min()), float(sub["value"].max())
        rng = st.slider("Value range", lo, hi, (lo, hi)) if hi > lo else (lo, hi)
        fam = st.text_input("Polymer family contains")
        dmax = st.number_input("Max Đ", value=None, min_value=1.0)
        res = retrieve(df, property_name=prop, value_min=rng[0], value_max=rng[1], polymer_family=fam or None,
                       dispersity_max=dmax, limit=100)
        st.dataframe(res, hide_index=True, alt="Matching verified records",
                     column_config={"doi": st.column_config.TextColumn("DOI"), "evidence_text": st.column_config.TextColumn("Evidence", width="large")})
    elif view == "Ask":
        q = st.text_input("Question", placeholder="Which polystyrene samples have Tg above 370 K and what are their Mn?")
        if q:
            ans = answer_with_evidence(df, q)
            st.markdown(ans.answer)
            st.caption(f"Generator: {ans.generator}. {ans.interpretation_label}")
            st.dataframe(ans.evidence, hide_index=True, alt="Evidence records used for the answer")
