"""
ParaBank Test Report Generator
--------------------------------
Reads a JMeter .jtl results file and produces TWO separate detailed PDF reports:
  1. Performance_Report.pdf   -> all HTTP samplers (Login, Open Account, Transfer, Bill Pay, Loan, Profile)
  2. Database_Report.pdf      -> all JDBC samplers (labels starting with "DB - ")

Each PDF includes:
  - Test date/time, duration, concurrent users (load)
  - Total requests, pass/fail counts, error %
  - Per-endpoint breakdown: count, avg/min/max response time, 90th/95th percentile, error %
  - Failure detail table: endpoint, HTTP code, reason, count, first-seen date/time
  - Charts: average response time per endpoint, error count per endpoint, throughput over time

REQUIREMENTS:
  pip install pandas matplotlib reportlab

USAGE:
  python generate_pdf_reports.py results.jtl

  If you ran two separate JMeter runs (one for performance-only, one DB-only),
  just point this script at the ONE combined .jtl that has everything -
  it auto-splits by label prefix "DB - " vs everything else. No need to
  pre-filter with -Jjmeter.reportgenerator.sample_filter at all.
"""

import sys
import os
from datetime import datetime

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


# ---------------- CONFIG ----------------
OUTPUT_DIR = "."
CHART_DPI = 130

DB_INFO = {
    "Database Type": "HSQLDB (embedded, HyperSQL)",
    "JDBC URL": "jdbc:hsqldb:hsql://localhost:9001/parabank",
    "Driver": "org.hsqldb.jdbcDriver",
    "Username": "sa",
}

# Exact queries configured in the JMeter JDBC samplers - shown in the report
# so it's clear which query ran for which module (JTL doesn't store SQL text
# by default, so this is documented here instead of re-enabling samplerData).
DB_QUERY_MAP = {
    "DB - Verify New Account": "SELECT ID, CUSTOMER_ID, TYPE, BALANCE FROM ACCOUNT WHERE CUSTOMER_ID = ? ORDER BY ID DESC;",
    "DB - Verify Bill Pay": "SELECT ID, ACCOUNT_ID, TYPE, DATE, AMOUNT, DESCRIPTION FROM TRANSACTION WHERE ACCOUNT_ID = ? AND TYPE = 1 ORDER BY ID DESC;",
    "DB - Verify Transfer": "SELECT ID, ACCOUNT_ID, TYPE, DATE, AMOUNT, DESCRIPTION FROM TRANSACTION WHERE ACCOUNT_ID IN (?, ?) ORDER BY ID DESC;",
    "DB - Verify Loan Account": "SELECT ID, CUSTOMER_ID, TYPE, BALANCE FROM ACCOUNT WHERE CUSTOMER_ID = ? ORDER BY ID DESC;",
    "DB - Verify Profile Update": "SELECT ID, FIRST_NAME, LAST_NAME, ADDRESS, CITY, STATE, ZIP_CODE, PHONE_NUMBER FROM CUSTOMER WHERE ID = ?;",
    "DB - Account Balance Check": "SELECT ID, CUSTOMER_ID, TYPE, BALANCE FROM ACCOUNT WHERE ID = ?;",
    "DB - Transfer Verification": "SELECT ID, ACCOUNT_ID, TYPE, DATE, AMOUNT, DESCRIPTION FROM TRANSACTION WHERE ACCOUNT_ID IN (?, ?) ORDER BY ID DESC;",
}
# -----------------------------------------


def load_jtl(path):
    df = pd.read_csv(path)
    # normalize success column to real boolean
    df["success"] = df["success"].astype(str).str.lower().isin(["true", "1"])
    df["datetime"] = pd.to_datetime(df["timeStamp"], unit="ms")
    return df


def split_perf_db(df):
    is_db = df["label"].astype(str).str.startswith("DB -")
    return df[~is_db].copy(), df[is_db].copy()


def overall_summary(df):
    total = len(df)
    passed = int(df["success"].sum())
    failed = total - passed
    fail_pct = round((failed / total) * 100, 2) if total else 0
    start = df["datetime"].min()
    end = df["datetime"].max()
    duration_sec = max((end - start).total_seconds(), 0.001)
    max_users = int(df["allThreads"].max()) if "allThreads" in df.columns else None
    throughput = round(total / duration_sec, 2)
    avg_rt = round(df["elapsed"].mean(), 1) if total else 0
    return {
        "total": total, "passed": passed, "failed": failed, "fail_pct": fail_pct,
        "start": start, "end": end, "duration_sec": round(duration_sec, 1),
        "max_users": max_users, "throughput": throughput, "avg_rt": avg_rt,
    }


def per_label_stats(df):
    rows = []
    for label, g in df.groupby("label"):
        count = len(g)
        passed = int(g["success"].sum())
        failed = count - passed
        fail_pct = round((failed / count) * 100, 2) if count else 0
        rows.append({
            "Endpoint": label,
            "Count": count,
            "Pass": passed,
            "Fail": failed,
            "Fail %": fail_pct,
            "Avg (ms)": round(g["elapsed"].mean(), 1),
            "Min (ms)": int(g["elapsed"].min()),
            "Max (ms)": int(g["elapsed"].max()),
            "90th pct (ms)": round(g["elapsed"].quantile(0.90), 1),
            "95th pct (ms)": round(g["elapsed"].quantile(0.95), 1),
        })
    return pd.DataFrame(rows).sort_values("Endpoint")


def failure_detail(df):
    fails = df[~df["success"]].copy()
    if fails.empty:
        return pd.DataFrame(columns=["Endpoint", "HTTP Code", "Reason", "Count", "First Seen"])
    reason_col = "responseMessage" if "responseMessage" in fails.columns else None
    group_cols = ["label", "responseCode"] + ([reason_col] if reason_col else [])
    grouped = fails.groupby(group_cols).agg(
        Count=("elapsed", "count"),
        First_Seen=("datetime", "min"),
    ).reset_index()
    grouped = grouped.rename(columns={
        "label": "Endpoint", "responseCode": "HTTP Code",
        reason_col: "Reason" if reason_col else "Reason",
        "First_Seen": "First Seen",
    })
    if reason_col is None:
        grouped["Reason"] = "Unknown (no responseMessage column in JTL)"
    grouped["First Seen"] = grouped["First Seen"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return grouped.sort_values("Count", ascending=False)


def query_pass_fail_summary(df):
    """For each DB module: total runs, success count, fail count, and the
    distinct failure reasons seen (JDBC exception text lands in responseMessage)."""
    rows = []
    for label, g in df.groupby("label"):
        total = len(g)
        fails = g[~g["success"]]
        passed = total - len(fails)
        reasons = "-"
        if len(fails):
            reason_col = "responseMessage" if "responseMessage" in fails.columns else None
            if reason_col:
                unique_reasons = fails[reason_col].dropna().unique().tolist()
                reasons = "; ".join(str(r) for r in unique_reasons[:3])
                if len(unique_reasons) > 3:
                    reasons += f" (+{len(unique_reasons)-3} more)"
        rows.append({
            "Module": label,
            "SQL Query": DB_QUERY_MAP.get(label, "(see JMeter JDBC sampler)"),
            "Total Runs": total,
            "Successful": passed,
            "Failed": len(fails),
            "Failure Reason(s)": reasons,
        })
    return pd.DataFrame(rows).sort_values("Module")
    plt.figure(figsize=(9, 4))
    plt.bar(stats_df["Endpoint"], stats_df["Avg (ms)"], color="#2c6fbb")
    plt.xticks(rotation=30, ha="right", fontsize=8)
    plt.ylabel("Avg Response Time (ms)")
    plt.title("Average Response Time per Endpoint")
    plt.tight_layout()
    plt.savefig(path, dpi=CHART_DPI)
    plt.close()


def chart_avg_response_time(stats_df, path):
    plt.figure(figsize=(9, 4))
    plt.bar(stats_df["Endpoint"], stats_df["Avg (ms)"], color="#2c6fbb")
    plt.xticks(rotation=30, ha="right", fontsize=8)
    plt.ylabel("Avg Response Time (ms)")
    plt.title("Average Response Time per Endpoint")
    plt.tight_layout()
    plt.savefig(path, dpi=CHART_DPI)
    plt.close()


def chart_errors_per_endpoint(stats_df, path):
    plt.figure(figsize=(9, 4))
    plt.bar(stats_df["Endpoint"], stats_df["Fail"], color="#c0392b")
    plt.xticks(rotation=30, ha="right", fontsize=8)
    plt.ylabel("Failed Requests")
    plt.title("Failures per Endpoint")
    plt.tight_layout()
    plt.savefig(path, dpi=CHART_DPI)
    plt.close()


def chart_throughput_over_time(df, path):
    ts = df.set_index("datetime").resample("1s").size()
    plt.figure(figsize=(9, 4))
    plt.plot(ts.index, ts.values, color="#27ae60")
    plt.ylabel("Requests / second")
    plt.title("Throughput Over Time (Load on the System)")
    plt.xticks(rotation=20, fontsize=8)
    plt.tight_layout()
    plt.savefig(path, dpi=CHART_DPI)
    plt.close()


def chart_response_vs_load(df, path):
    g = df.set_index("datetime").resample("1min")
    avg_rt = g["elapsed"].mean()
    p90_rt = g["elapsed"].quantile(0.90)
    users = g["allThreads"].max() if "allThreads" in df.columns else None

    fig, ax1 = plt.subplots(figsize=(9, 4))
    ax1.plot(avg_rt.index, avg_rt.values, label="Avg response time (ms)", color="#2c6fbb")
    ax1.plot(p90_rt.index, p90_rt.values, label="P90 response time (ms)", color="#e67e22", linestyle="--")
    ax1.set_ylabel("Response time (ms)")
    ax1.set_xlabel("Time")
    if users is not None:
        ax2 = ax1.twinx()
        ax2.plot(users.index, users.values, label="Concurrent users", color="#27ae60", alpha=0.6)
        ax2.set_ylabel("Concurrent users")
    fig.legend(loc="upper left", bbox_to_anchor=(0.1, 0.88), fontsize=8)
    plt.title("Response Time vs Concurrent Load Over Time")
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(path, dpi=CHART_DPI)
    plt.close()


def chart_error_rate_over_time(df, path):
    g = df.set_index("datetime").resample("1min")
    total = g.size()
    failed = g.apply(lambda x: (~x["success"]).sum())
    err_pct = (failed / total * 100).fillna(0)
    plt.figure(figsize=(9, 3.5))
    plt.plot(err_pct.index, err_pct.values, color="#c0392b", marker="o", markersize=3)
    plt.ylabel("Error rate (%)")
    plt.title("Error Rate Over Time")
    plt.xticks(rotation=20, fontsize=8)
    plt.tight_layout()
    plt.savefig(path, dpi=CHART_DPI)
    plt.close()


def time_series_table(df):
    g = df.set_index("datetime").resample("1min")
    rows = []
    for ts, chunk in g:
        if len(chunk) == 0:
            continue
        total = len(chunk)
        failed = int((~chunk["success"]).sum())
        users_avg = round(chunk["allThreads"].mean(), 1) if "allThreads" in chunk.columns else None
        users_max = int(chunk["allThreads"].max()) if "allThreads" in chunk.columns else None
        rows.append({
            "Date/Time": ts.strftime("%d-%b %H:%M"),
            "Requests": total,
            "Avg RT (ms)": round(chunk["elapsed"].mean(), 1),
            "P90 RT (ms)": round(chunk["elapsed"].quantile(0.90), 1),
            "Max RT (ms)": int(chunk["elapsed"].max()),
            "Users (avg/max)": f"{users_avg} / {users_max}",
            "Error %": round((failed / total) * 100, 2),
            "Throughput/s": round(total / 60, 2),
        })
    return pd.DataFrame(rows)


def df_to_table(df, col_widths=None):
    data = [list(df.columns)] + df.astype(str).values.tolist()
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
    ]))
    return t


def build_pdf(title, df, out_path, is_db_report):
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleBig", parent=styles["Title"], fontSize=20)
    h2 = styles["Heading2"]
    normal = styles["Normal"]

    summ = overall_summary(df)
    stats = per_label_stats(df)
    fails = failure_detail(df)

    tag = "db" if is_db_report else "perf"
    tmp_dir = "/tmp/report_charts"
    os.makedirs(tmp_dir, exist_ok=True)
    chart1 = os.path.join(tmp_dir, f"{tag}_avg_rt.png")
    chart2 = os.path.join(tmp_dir, f"{tag}_errors.png")
    chart3 = os.path.join(tmp_dir, f"{tag}_throughput.png")
    chart4 = os.path.join(tmp_dir, f"{tag}_load.png")
    chart5 = os.path.join(tmp_dir, f"{tag}_errrate.png")
    chart_avg_response_time(stats, chart1)
    chart_errors_per_endpoint(stats, chart2)
    chart_throughput_over_time(df, chart3)
    chart_response_vs_load(df, chart4)
    chart_error_rate_over_time(df, chart5)
    ts_table = time_series_table(df)

    doc = SimpleDocTemplate(out_path, pagesize=landscape(A4),
                             topMargin=1.2*cm, bottomMargin=1.2*cm,
                             leftMargin=1.2*cm, rightMargin=1.2*cm)
    elems = []

    elems.append(Paragraph(title, title_style))
    elems.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal))
    elems.append(Spacer(1, 12))

    # Summary section
    elems.append(Paragraph("Test Summary (Load Overview)", h2))
    summary_table_data = pd.DataFrame([
        {"Metric": "Test Start", "Value": summ["start"].strftime("%Y-%m-%d %H:%M:%S")},
        {"Metric": "Test End", "Value": summ["end"].strftime("%Y-%m-%d %H:%M:%S")},
        {"Metric": "Duration (sec)", "Value": summ["duration_sec"]},
        {"Metric": "Max Concurrent Users (Load)", "Value": summ["max_users"]},
        {"Metric": "Total Requests", "Value": summ["total"]},
        {"Metric": "Passed", "Value": summ["passed"]},
        {"Metric": "Failed", "Value": summ["failed"]},
        {"Metric": "Error Rate %", "Value": summ["fail_pct"]},
        {"Metric": "Throughput (req/sec)", "Value": summ["throughput"]},
        {"Metric": "Overall Avg Response Time (ms)", "Value": summ["avg_rt"]},
    ])
    elems.append(df_to_table(summary_table_data, col_widths=[7*cm, 6*cm]))
    elems.append(Spacer(1, 14))

    if is_db_report:
        elems.append(Paragraph("Database Info", h2))
        db_info_df = pd.DataFrame([{"Property": k, "Value": v} for k, v in DB_INFO.items()])
        elems.append(df_to_table(db_info_df, col_widths=[6*cm, 14*cm]))
        elems.append(Spacer(1, 10))

        elems.append(Paragraph("Queries Executed per Module", h2))
        q_rows = []
        for label in sorted(df["label"].unique()):
            q_rows.append({"Module": label, "SQL Query": DB_QUERY_MAP.get(label, "(not documented - check JMeter JDBC sampler)")})
        elems.append(df_to_table(pd.DataFrame(q_rows), col_widths=[6*cm, 22*cm]))
        elems.append(Spacer(1, 14))

        elems.append(Paragraph("Which Queries Passed / Failed, and Why", h2))
        qpf = query_pass_fail_summary(df)
        elems.append(df_to_table(qpf, col_widths=[4.5*cm, 7*cm, 2.2*cm, 2.2*cm, 2.2*cm, 8*cm]))
        elems.append(Spacer(1, 14))

    # Per-endpoint stats
    label_col_name = "DB Query / Module" if is_db_report else "API Endpoint / Module"
    stats_renamed = stats.rename(columns={"Endpoint": label_col_name})
    elems.append(Paragraph("Per-Endpoint / Per-Module Breakdown", h2))
    elems.append(df_to_table(stats_renamed))
    elems.append(Spacer(1, 14))

    elems.append(PageBreak())
    elems.append(Paragraph("Response Time vs Concurrent Load Over Time", h2))
    elems.append(Image(chart4, width=24*cm, height=10*cm))
    elems.append(Spacer(1, 10))
    elems.append(Paragraph("Error Rate Over Time", h2))
    elems.append(Image(chart5, width=24*cm, height=8*cm))
    elems.append(PageBreak())
    elems.append(Paragraph("Average Response Time per Endpoint", h2))
    elems.append(Image(chart1, width=24*cm, height=10*cm))
    elems.append(Spacer(1, 10))
    elems.append(Paragraph("Failures per Endpoint", h2))
    elems.append(Image(chart2, width=24*cm, height=10*cm))
    elems.append(PageBreak())
    elems.append(Paragraph("Throughput Over Time (System Load)", h2))
    elems.append(Image(chart3, width=24*cm, height=10*cm))
    elems.append(Spacer(1, 14))

    elems.append(PageBreak())
    elems.append(Paragraph("Time-wise Load & Performance Detail (per minute)", h2))
    if not ts_table.empty:
        elems.append(df_to_table(ts_table))
    elems.append(Spacer(1, 14))

    elems.append(PageBreak())
    elems.append(Paragraph("Failure Detail (Reasons, Codes, When)", h2))
    if fails.empty:
        elems.append(Paragraph("No failures recorded in this run.", normal))
    else:
        elems.append(df_to_table(fails.rename(columns={"Endpoint": label_col_name})))

    doc.build(elems)
    print(f"Saved: {out_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_pdf_reports.py <path-to-results.jtl>")
        sys.exit(1)

    jtl_path = sys.argv[1]
    df = load_jtl(jtl_path)
    perf_df, db_df = split_perf_db(df)

    if not perf_df.empty:
        build_pdf("ParaBank Performance Test Report", perf_df,
                   os.path.join(OUTPUT_DIR, "Performance_Report.pdf"), is_db_report=False)
    else:
        print("No HTTP (performance) samples found in this JTL.")

    if not db_df.empty:
        build_pdf("ParaBank Database Test Report", db_df,
                   os.path.join(OUTPUT_DIR, "Database_Report.pdf"), is_db_report=True)
    else:
        print("No DB samples found in this JTL (labels starting with 'DB - ').")


if __name__ == "__main__":
    main()
