"""Streamlit interface for the product workbook scraper."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
import streamlit as st

from scraper import (
    AUTO_ENGINE,
    MAX_CONCURRENCY,
    results_to_frame,
    scrape_workbook_rows,
)

st.set_page_config(page_title="Catalog Extractor", page_icon=":material/travel_explore:", layout="wide")
st.markdown(
    """
    <style>
    :root { --ink: #202b32; --muted: #59676e; --accent: #0d6b5d; --line: #d7e0dc; }
    .stApp { background: linear-gradient(160deg, #f5f8f5 0%, #eef3f0 48%, #f7f7f2 100%); color: var(--ink); }
    h1, h2, h3 { color: var(--ink); }
    section[data-testid="stSidebar"] { background: #e8efea; }
    div[data-testid="stFileUploader"] { border: 1px solid var(--line); border-radius: 6px; }
    div.stButton > button[kind="primary"] { background: var(--accent); border-color: var(--accent); }
    div[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 6px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Catalog Extractor")
st.caption("Product titles, highlights, categories, descriptions, and specifications from SKU/URL workbooks.")

with st.sidebar:
    st.subheader("Browser")
    engine = st.selectbox(
        "Engine",
        (
            AUTO_ENGINE,
            "Crawl4AI",
            "CloakBrowser",
            "Scrapling Stealth",
        ),
    )
    concurrency = st.slider(
        "Concurrent products",
        min_value=1,
        max_value=MAX_CONCURRENCY,
        value=4,
        help="Start at 4. Lower this if you see rate limits or browser resource pressure.",
    )
    st.caption("Use only on sites you are authorized to access. Browser protection bypass is not guaranteed.")

template = BytesIO()
pd.DataFrame(columns=["SKU", "URL"]).to_excel(template, index=False, engine="openpyxl")
st.download_button(
    "Download input template",
    data=template.getvalue(),
    file_name="sku_url_template.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    icon=":material/download:",
)

uploaded_file = st.file_uploader("Input workbook (.xlsx)", type=["xlsx"])
if uploaded_file is not None:
    try:
        input_frame = pd.read_excel(uploaded_file, dtype=str, engine="openpyxl")
        st.caption(f"{len(input_frame):,} workbook rows loaded")
    except Exception as exc:
        input_frame = None
        st.error(f"Could not read this workbook: {exc}")
else:
    input_frame = None

metrics_placeholder = st.empty()
results_placeholder = st.empty()

if st.button(
    "Run extraction",
    type="primary",
    icon=":material/play_arrow:",
    disabled=input_frame is None,
):
    progress_bar = st.progress(0, text="Starting browser")
    completed_rows: dict[int, dict] = {}
    preview_caption = st.empty()

    def update_progress(
        done: int,
        total: int,
        message: str,
        row_index: int,
        completed_result: dict,
    ) -> None:
        progress_bar.progress(done / total, text=message)
        completed_rows.pop(row_index, None)
        completed_rows[row_index] = completed_result
        latest_results = list(completed_rows.values())[-50:]
        preview_caption.caption(
            f"Showing the latest {len(latest_results)} completed products while scraping."
        )
        results_placeholder.dataframe(
            results_to_frame(latest_results),
            use_container_width=True,
            hide_index=True,
        )

    try:
        st.session_state["scrape_results"] = scrape_workbook_rows(
            input_frame,
            engine=engine,
            concurrency=concurrency,
            progress=update_progress,
        )
    except Exception as exc:
        st.error(str(exc))
        st.session_state.pop("scrape_results", None)

results = st.session_state.get("scrape_results")
if results is not None:
    successful = int((results["Status"] == "Success").sum())
    failed = len(results) - successful
    with metrics_placeholder.container():
        first_metric, second_metric, third_metric = st.columns(3)
        first_metric.metric("Products", len(results))
        second_metric.metric("Extracted", successful)
        third_metric.metric("Needs attention", failed)
    results_placeholder.dataframe(results, use_container_width=True, hide_index=True)

    workbook = BytesIO()
    results.to_excel(workbook, index=False, engine="openpyxl")
    st.download_button(
        "Download results",
        data=workbook.getvalue(),
        file_name="product_extraction.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
    )