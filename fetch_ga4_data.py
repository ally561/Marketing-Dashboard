#!/usr/bin/env python3
"""
Digital Brew Marketing Dashboard — Google Analytics 4 fetcher.

Pulls traffic, channel, conversion, and landing-page data from GA4 via the
Analytics Data API, and merges it into docs/data.json alongside the HubSpot
funnel data (run fetch_hubspot_data.py first).

Requires:
  GA4_PROPERTY_ID           e.g. "123456789" (just the number, no "properties/" prefix)
  GA4_SERVICE_ACCOUNT_JSON  the full JSON key file contents, as a single string

Run manually:
    GA4_PROPERTY_ID=123456789 GA4_SERVICE_ACCOUNT_JSON="$(cat key.json)" python fetch_ga4_data.py

Runs automatically via .github/workflows/update-dashboard.yml
"""

import os
import sys
import json
import statistics
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest, OrderBy,
)

PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
SERVICE_ACCOUNT_JSON = os.environ.get("GA4_SERVICE_ACCOUNT_JSON")

TRAILING_WEEKS = 12          # keep in sync with fetch_hubspot_data.py
CHANNEL_WINDOW_DAYS = 45     # keep in sync with SOURCE_WINDOW_DAYS
LANDING_PAGE_WINDOW_DAYS = 45
LANDING_PAGE_LIMIT = 10

DATA_JSON_PATH = os.path.join(os.path.dirname(__file__), "docs", "data.json")


def get_client():
    if not PROPERTY_ID or not SERVICE_ACCOUNT_JSON:
        sys.exit("GA4_PROPERTY_ID and GA4_SERVICE_ACCOUNT_JSON environment variables are required.")
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=creds)


def week_start(dt):
    d = dt.date() if isinstance(dt, datetime) else dt
    return d - timedelta(days=d.weekday())


def bucket_weekly_from_ga_dates(rows_with_date_and_value, weeks_back):
    """rows_with_date_and_value: list of (yyyymmdd_str, value)."""
    now = datetime.now(timezone.utc)
    earliest = week_start(now - timedelta(weeks=weeks_back))
    buckets = defaultdict(float)
    for ds, val in rows_with_date_and_value:
        dt = datetime.strptime(ds, "%Y%m%d").replace(tzinfo=timezone.utc)
        wk = week_start(dt)
        if wk >= earliest:
            buckets[wk] += val
    ordered = []
    cur = earliest
    today_week = week_start(now)
    while cur <= today_week:
        ordered.append({
            "week": cur.isoformat(),
            "sessions": round(buckets.get(cur, 0)),
            "partial": cur == today_week,
        })
        cur += timedelta(days=7)
    return ordered


def fetch_weekly_traffic(client, weeks_back):
    start = (datetime.now(timezone.utc) - timedelta(weeks=weeks_back + 1)).strftime("%Y-%m-%d")
    req = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="sessions"), Metric(name="totalUsers"), Metric(name="conversions")],
        date_ranges=[DateRange(start_date=start, end_date="today")],
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
    )
    resp = client.run_report(req)
    sessions_rows, users_rows, conv_rows = [], [], []
    for row in resp.rows:
        d = row.dimension_values[0].value
        sessions_rows.append((d, float(row.metric_values[0].value)))
        users_rows.append((d, float(row.metric_values[1].value)))
        conv_rows.append((d, float(row.metric_values[2].value)))
    sessions_weekly = bucket_weekly_from_ga_dates(sessions_rows, weeks_back)
    users_weekly = bucket_weekly_from_ga_dates(users_rows, weeks_back)
    conv_weekly = bucket_weekly_from_ga_dates(conv_rows, weeks_back)
    # merge into one list keyed by week
    merged = []
    for s, u, c in zip(sessions_weekly, users_weekly, conv_weekly):
        merged.append({
            "week": s["week"],
            "sessions": s["sessions"],
            "users": u["sessions"],
            "conversions": c["sessions"],
            "partial": s["partial"],
        })
    return merged


def fetch_channel_breakdown(client, days):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    req = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="sessionDefaultChannelGroup")],
        metrics=[Metric(name="sessions"), Metric(name="conversions")],
        date_ranges=[DateRange(start_date=start, end_date="today")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
    )
    resp = client.run_report(req)
    out = {}
    for row in resp.rows:
        channel = row.dimension_values[0].value or "Unassigned"
        out[channel] = {
            "sessions": int(float(row.metric_values[0].value)),
            "conversions": round(float(row.metric_values[1].value), 1),
        }
    return out


def fetch_landing_pages(client, days, limit):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    req = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="landingPage")],
        metrics=[Metric(name="sessions"), Metric(name="engagementRate"), Metric(name="conversions")],
        date_ranges=[DateRange(start_date=start, end_date="today")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(req)
    rows = []
    for row in resp.rows:
        rows.append({
            "page": row.dimension_values[0].value,
            "sessions": int(float(row.metric_values[0].value)),
            "engagement_rate_pct": round(float(row.metric_values[1].value) * 100, 1),
            "conversions": round(float(row.metric_values[2].value), 1),
        })
    return rows


def trailing_avg(weekly, n, key="sessions", exclude_partial=True):
    vals = [w[key] for w in weekly if not (exclude_partial and w.get("partial"))]
    vals = vals[-n:]
    return round(statistics.mean(vals), 1) if vals else 0


def build_ga_suggestions(weekly_traffic, channel_breakdown, landing_pages):
    s = []

    total_sessions = sum(c["sessions"] for c in channel_breakdown.values()) or 1
    conv_avg4 = trailing_avg(weekly_traffic, 4, key="conversions")
    sessions_avg4 = trailing_avg(weekly_traffic, 4, key="sessions")

    if sessions_avg4:
        conv_rate = round((conv_avg4 / sessions_avg4) * 100, 2)
        if conv_rate < 1:
            s.append({
                "severity": "medium",
                "title": "Site conversion rate is under 1%",
                "detail": f"Trailing 4-week average is ~{sessions_avg4} sessions/week producing ~{conv_avg4} "
                          f"conversions/week ({conv_rate}%). Compare this against the MQL numbers from HubSpot — "
                          f"if traffic is healthy but conversions are thin, the constraint is on-site (offer, "
                          f"forms, landing pages), not top-of-funnel volume."
            })

    paid = channel_breakdown.get("Paid Search", {}).get("sessions", 0)
    if total_sessions and (paid / total_sessions) < 0.15:
        s.append({
            "severity": "low",
            "title": "Paid Search is a small share of site sessions too",
            "detail": f"Paid Search drove only {paid} of {total_sessions} sessions in the last {CHANNEL_WINDOW_DAYS} "
                      f"days ({round(paid/total_sessions*100, 1)}%). This matches the low Paid Search share seen in "
                      f"HubSpot's MQL source data — worth confirming Google Ads is actually serving impressions "
                      f"before assuming the funnel itself is the problem."
        })

    if landing_pages:
        low_engagement = [p for p in landing_pages if p["engagement_rate_pct"] < 40 and p["sessions"] >= 20]
        if low_engagement:
            worst = sorted(low_engagement, key=lambda p: p["sessions"], reverse=True)[0]
            s.append({
                "severity": "medium",
                "title": "A high-traffic landing page has low engagement",
                "detail": f"'{worst['page']}' got {worst['sessions']} sessions but only "
                          f"{worst['engagement_rate_pct']}% engagement rate in the last {LANDING_PAGE_WINDOW_DAYS} "
                          f"days. High traffic + low engagement usually means a messaging or page-speed mismatch "
                          f"with what drove the click — worth reviewing before spending more to send traffic there."
            })

    return s


def main():
    client = get_client()

    print("Fetching weekly traffic (sessions/users/conversions)...")
    weekly_traffic = fetch_weekly_traffic(client, TRAILING_WEEKS)

    print("Fetching channel breakdown...")
    channel_breakdown = fetch_channel_breakdown(client, CHANNEL_WINDOW_DAYS)

    print("Fetching landing page performance...")
    landing_pages = fetch_landing_pages(client, LANDING_PAGE_WINDOW_DAYS, LANDING_PAGE_LIMIT)

    ga_suggestions = build_ga_suggestions(weekly_traffic, channel_breakdown, landing_pages)

    ga_block = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "weekly_traffic": weekly_traffic,
        "sessions_avg_4wk": trailing_avg(weekly_traffic, 4, key="sessions"),
        "conversions_avg_4wk": trailing_avg(weekly_traffic, 4, key="conversions"),
        "channel_breakdown": channel_breakdown,
        "landing_pages": landing_pages,
        "channel_window_days": CHANNEL_WINDOW_DAYS,
        "landing_page_window_days": LANDING_PAGE_WINDOW_DAYS,
    }

    if not os.path.exists(DATA_JSON_PATH):
        sys.exit(f"{DATA_JSON_PATH} not found — run fetch_hubspot_data.py first.")

    with open(DATA_JSON_PATH) as f:
        data = json.load(f)

    data["ga4"] = ga_block
    data.setdefault("suggestions", [])
    data["suggestions"].extend(ga_suggestions)

    with open(DATA_JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Merged GA4 data into {DATA_JSON_PATH}")


if __name__ == "__main__":
    main()
