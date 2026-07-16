#!/usr/bin/env python3
"""
Digital Brew Marketing Dashboard — HubSpot data fetcher.

Pulls contact lifecycle-stage history and deal data from HubSpot, computes
funnel/yield metrics, generates rules-based marketing-director suggestions,
and writes everything to docs/data.json for the static dashboard to render.

Requires env var HUBSPOT_TOKEN (a HubSpot Private App access token with
crm.objects.contacts.read and crm.objects.deals.read scopes).

Run manually:
    HUBSPOT_TOKEN=xxxx python fetch_hubspot_data.py

Runs automatically via .github/workflows/update-dashboard.yml
"""

import os
import sys
import json
import statistics
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN")
BASE_URL = "https://api.hubapi.com"

# ---- Config: edit these to match your goals / definitions ----------------
WEEKLY_MQL_GOAL = 25
WEEKLY_SQL_GOAL = 10
TRAILING_WEEKS = 53          # ~1 year of history, so the dashboard's date-range selector
                             # (this week / 30 / 60 / 90 days / quarter / YTD) always has enough data
SQL_TO_DEAL_WINDOW_DAYS = 120  # how far back to look when measuring SQL -> deal rate
SOURCE_WINDOW_DAYS = 45        # window for the "source of MQLs" breakdown

LIFECYCLE_LABELS = {
    "1064277769": "Unqualified",
    "lead": "Lead",
    "marketingqualifiedlead": "Marketing Qualified Lead",
    "salesqualifiedlead": "Sales Qualified Lead",
    "1066742671": "Nurture",
    "opportunity": "Opportunity",
    "1066867322": "Lost / Recycle",
    "customer": "Customer",
    "evangelist": "Priority Engaged",
    "other": "Opportunity Booked",
    "1170945711": "Vendor",
    "1374161474": "Agency",
}

SOURCE_LABELS = {
    "ORGANIC_SEARCH": "Organic Search",
    "PAID_SEARCH": "Paid Search",
    "EMAIL_MARKETING": "Email Marketing",
    "SOCIAL_MEDIA": "Organic Social",
    "REFERRALS": "Referrals",
    "OTHER_CAMPAIGNS": "Other Campaigns",
    "DIRECT_TRAFFIC": "Direct Traffic",
    "OFFLINE": "Offline Sources",
    "PAID_SOCIAL": "Paid Social",
    "AI_REFERRALS": "AI Referrals",
}

DEAL_STAGE_LABELS = {
    "1065773825": "Need Quote",
    "1065773826": "Quote Ready",
    "1065824343": "Quote Presented",
    "decisionmakerboughtin": "Decision Maker Bought-In",
    "1065824354": "Agreement Sent",
    "1133742776": "Maybe Later",
    "closedwon": "Closed Won",
    "closedlost": "Closed Lost",
}


def _headers():
    if not HUBSPOT_TOKEN:
        sys.exit("HUBSPOT_TOKEN environment variable is not set.")
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def search(object_type, filters, properties, limit=200, max_pages=50):
    """Paginate through /crm/v3/objects/{type}/search with a simple AND filter group."""
    url = f"{BASE_URL}/crm/v3/objects/{object_type}/search"
    results = []
    after = None
    for _ in range(max_pages):
        body = {
            "filterGroups": [{"filters": filters}] if filters else [],
            "properties": properties,
            "limit": limit,
        }
        if after:
            body["after"] = after
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        paging = data.get("paging", {}).get("next", {}).get("after")
        if not paging:
            break
        after = paging
    return results


def iso(dt):
    return dt.strftime("%Y-%m-%dT00:00:00Z")


def week_start(dt):
    """Return the Monday of the week containing dt, as a date."""
    d = dt.date() if isinstance(dt, datetime) else dt
    return d - timedelta(days=d.weekday())


def bucket_weekly(date_strings, weeks_back):
    """Given a list of ISO date strings, bucket counts by week (Mon-start)."""
    now = datetime.now(timezone.utc)
    earliest = week_start(now - timedelta(weeks=weeks_back))
    buckets = defaultdict(int)
    for ds in date_strings:
        if not ds:
            continue
        dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        wk = week_start(dt)
        if wk >= earliest:
            buckets[wk] += 1
    # Fill in every week (including zero weeks) so the chart has no gaps
    ordered = []
    cur = earliest
    today_week = week_start(now)
    while cur <= today_week:
        ordered.append({
            "week": cur.isoformat(),
            "count": buckets.get(cur, 0),
            "partial": cur == today_week,
        })
        cur += timedelta(days=7)
    return ordered


def bucket_weekly_amount(rows, date_key, amount_key, weeks_back):
    """Bucket (count, amount) by week for deal-style rows with a date + amount property."""
    now = datetime.now(timezone.utc)
    earliest = week_start(now - timedelta(weeks=weeks_back))
    buckets = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for r in rows:
        ds = r.get(date_key)
        if not ds:
            continue
        dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        wk = week_start(dt)
        if wk >= earliest:
            buckets[wk]["count"] += 1
            buckets[wk]["amount"] += float(r.get(amount_key) or 0)
    ordered = []
    cur = earliest
    today_week = week_start(now)
    while cur <= today_week:
        b = buckets.get(cur, {"count": 0, "amount": 0.0})
        ordered.append({
            "week": cur.isoformat(),
            "count": b["count"],
            "amount": round(b["amount"], 2),
            "partial": cur == today_week,
        })
        cur += timedelta(days=7)
    return ordered


def fetch_lifecycle_snapshot():
    """Current count of contacts in each lifecycle stage (year to date)."""
    year_start = iso(datetime(datetime.now(timezone.utc).year, 1, 1))
    snapshot = {}
    for stage, label in LIFECYCLE_LABELS.items():
        filters = [
            {"propertyName": "lifecyclestage", "operator": "EQ", "value": stage},
            {"propertyName": "createdate", "operator": "GTE", "value": year_start},
        ]
        url = f"{BASE_URL}/crm/v3/objects/contacts/search"
        resp = requests.post(url, headers=_headers(), json={
            "filterGroups": [{"filters": filters}],
            "limit": 1,
        }, timeout=30)
        resp.raise_for_status()
        snapshot[label] = resp.json().get("total", 0)
    return snapshot


def fetch_weekly_entries(date_property, weeks_back):
    cutoff = iso(datetime.now(timezone.utc) - timedelta(weeks=weeks_back + 1))
    filters = [{"propertyName": date_property, "operator": "GTE", "value": cutoff}]
    rows = search("contacts", filters, [date_property])
    dates = [r["properties"].get(date_property) for r in rows]
    return bucket_weekly(dates, weeks_back)


def fetch_sql_to_deal_rate(window_days):
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=window_days))
    filters = [
        {"propertyName": "lifecyclestage", "operator": "EQ", "value": "salesqualifiedlead"},
        {"propertyName": "hs_v2_date_entered_salesqualifiedlead", "operator": "GTE", "value": cutoff},
    ]
    rows = search("contacts", filters, ["num_associated_deals"])
    total = len(rows)
    with_deal = sum(1 for r in rows if (r["properties"].get("num_associated_deals") or "0") not in ("0", "", None))
    rate = round((with_deal / total) * 100, 1) if total else None
    return {"sql_count": total, "sql_with_deal": with_deal, "rate_pct": rate, "window_days": window_days}


def fetch_source_breakdown(window_days):
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=window_days))
    filters = [{"propertyName": "hs_v2_date_entered_marketingqualifiedlead", "operator": "GTE", "value": cutoff}]
    rows = search("contacts", filters, ["hs_analytics_source"])
    counts = defaultdict(int)
    for r in rows:
        src = r["properties"].get("hs_analytics_source") or "UNKNOWN"
        counts[SOURCE_LABELS.get(src, src)] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def fetch_deal_summary():
    year_start = iso(datetime(datetime.now(timezone.utc).year, 1, 1))
    filters = [{"propertyName": "createdate", "operator": "GTE", "value": year_start}]
    rows = search("deals", filters, [
        "dealstage", "amount_in_home_currency", "createdate",
        "hs_v2_date_entered_closedwon", "hs_v2_date_entered_closedlost",
    ])
    by_stage = defaultdict(lambda: {"count": 0, "amount": 0.0})
    created_dates = []
    won_rows, lost_rows = [], []
    for r in rows:
        p = r["properties"]
        stage = DEAL_STAGE_LABELS.get(p.get("dealstage"), p.get("dealstage") or "Unknown")
        amt = float(p.get("amount_in_home_currency") or 0)
        by_stage[stage]["count"] += 1
        by_stage[stage]["amount"] += amt
        created_dates.append(p.get("createdate"))
        if p.get("hs_v2_date_entered_closedwon"):
            won_rows.append({"date": p["hs_v2_date_entered_closedwon"], "amount": amt})
        if p.get("hs_v2_date_entered_closedlost"):
            lost_rows.append({"date": p["hs_v2_date_entered_closedlost"], "amount": amt})

    weekly_created = bucket_weekly(created_dates, TRAILING_WEEKS)
    weekly_closed_won = bucket_weekly_amount(won_rows, "date", "amount", TRAILING_WEEKS)
    weekly_closed_lost = bucket_weekly_amount(lost_rows, "date", "amount", TRAILING_WEEKS)

    won = by_stage.get("Closed Won", {"count": 0, "amount": 0})
    lost = by_stage.get("Closed Lost", {"count": 0, "amount": 0})
    win_rate = None
    closed_total = won["count"] + lost["count"]
    if closed_total:
        win_rate = round((won["count"] / closed_total) * 100, 1)
    open_pipeline = sum(v["amount"] for k, v in by_stage.items() if k not in ("Closed Won", "Closed Lost"))
    return {
        "by_stage": {k: {"count": v["count"], "amount": round(v["amount"], 2)} for k, v in by_stage.items()},
        "weekly_created": weekly_created,
        "weekly_closed_won": weekly_closed_won,
        "weekly_closed_lost": weekly_closed_lost,
        "closed_won_count": won["count"],
        "closed_won_revenue": round(won["amount"], 2),
        "closed_lost_count": lost["count"],
        "win_rate_pct": win_rate,
        "open_pipeline_value": round(open_pipeline, 2),
    }


def trailing_avg(weekly, n, exclude_partial=True):
    vals = [w["count"] for w in weekly if not (exclude_partial and w.get("partial"))]
    vals = vals[-n:]
    return round(statistics.mean(vals), 1) if vals else 0


def detect_anomaly_weeks(weekly, factor=2.5):
    """Flag weeks whose count is far above the trailing median (e.g. bulk imports)."""
    counts = [w["count"] for w in weekly if w["count"] > 0]
    if len(counts) < 4:
        return []
    med = statistics.median(counts)
    flagged = []
    for w in weekly:
        if med > 0 and w["count"] > med * factor and w["count"] > 20:
            flagged.append(w["week"])
    return flagged


def build_suggestions(mql_weekly, sql_weekly, sql_deal, deals, source_breakdown, mql_anomalies, sql_anomalies):
    s = []

    mql_avg4 = trailing_avg(mql_weekly, 4)
    sql_avg4 = trailing_avg(sql_weekly, 4)

    # 1. Weekly goal vs demonstrated capacity
    if mql_avg4 and mql_avg4 < WEEKLY_MQL_GOAL * 0.7:
        gap = round((WEEKLY_MQL_GOAL - mql_avg4) / max(mql_avg4, 1) * 100)
        s.append({
            "severity": "high",
            "title": "Weekly MQL goal is well above current capacity",
            "detail": f"Trailing 4-week average is {mql_avg4} MQLs/week vs. a goal of {WEEKLY_MQL_GOAL} "
                      f"(a {gap}% gap). Treat this as a capacity/investment conversation with leadership, "
                      f"and report progress on a rolling 4-week basis rather than judging single weeks."
        })
    if sql_avg4 and sql_avg4 < WEEKLY_SQL_GOAL * 0.7:
        gap = round((WEEKLY_SQL_GOAL - sql_avg4) / max(sql_avg4, 1) * 100)
        s.append({
            "severity": "high",
            "title": "Weekly SQL goal is well above current capacity",
            "detail": f"Trailing 4-week average is {sql_avg4} SQLs/week vs. a goal of {WEEKLY_SQL_GOAL} "
                      f"(a {gap}% gap). Consider proposing an interim target (e.g. 60% of goal) while you "
                      f"fix the constraints below."
        })

    # 2. Bulk-import / data anomalies skewing averages
    if mql_anomalies or sql_anomalies:
        weeks = sorted(set(mql_anomalies + sql_anomalies))
        s.append({
            "severity": "medium",
            "title": "One or more weeks look like data anomalies, not organic funnel activity",
            "detail": f"Week(s) starting {', '.join(weeks)} show MQL/SQL counts far above the trailing median "
                      f"— check for bulk imports, list re-syncs, or lifecycle-stage backfills. Left in, these "
                      f"spikes make your real weekly trend look better (or worse) than it is."
        })

    # 3. SQL -> deal tracking gap
    if sql_deal["rate_pct"] is not None and sql_deal["rate_pct"] < 70:
        s.append({
            "severity": "high",
            "title": "A meaningful share of SQLs never get a deal created",
            "detail": f"Only {sql_deal['sql_with_deal']} of {sql_deal['sql_count']} SQLs "
                      f"({sql_deal['rate_pct']}%) from the last {sql_deal['window_days']} days have an "
                      f"associated deal. Add a workflow that flags SQLs with no deal after 14 days — this "
                      f"is often a bigger lever than generating more top-of-funnel leads."
        })

    # 4. Deal win rate
    if deals["win_rate_pct"] is not None and deals["win_rate_pct"] < 50:
        s.append({
            "severity": "medium",
            "title": "Win rate on closed deals is below 50%",
            "detail": f"{deals['closed_won_count']} won vs. {deals['closed_lost_count']} lost "
                      f"({deals['win_rate_pct']}% win rate). Pull the last 20-30 closed-lost deals and "
                      f"classify by reason (timing, budget, competitor, no response) before spending more "
                      f"on acquisition — a qualification or sales-process fix may be worth more than new leads."
        })

    # 5. Source concentration / attribution risk
    total_src = sum(source_breakdown.values()) or 1
    top_source, top_count = (list(source_breakdown.items())[0] if source_breakdown else (None, 0))
    if top_source and (top_count / total_src) > 0.5:
        s.append({
            "severity": "medium" if top_source in ("Offline Sources", "Direct Traffic") else "low",
            "title": f"'{top_source}' accounts for over half of recent MQLs",
            "detail": f"{top_source} produced {top_count} of {total_src} MQLs "
                      f"({round(top_count/total_src*100)}%) in the last window. If that's 'Offline Sources' "
                      f"or 'Direct Traffic', it usually means attribution is incomplete rather than that "
                      f"channel actually performing best — worth auditing tracking before reallocating budget."
        })

    # 6. Paid search specifically underperforming
    paid = source_breakdown.get("Paid Search", 0)
    if total_src and paid / total_src < 0.15:
        s.append({
            "severity": "low",
            "title": "Paid Search is a small share of recent MQLs",
            "detail": f"Paid Search produced only {paid} of {total_src} recent MQLs. If Google Ads spend "
                      f"hasn't dropped proportionally, cost per lead is likely climbing — review search terms "
                      f"and consider shifting Smart Bidding to optimize on MQL/SQL value rather than raw form fills."
        })

    if not s:
        s.append({
            "severity": "low",
            "title": "No major red flags this run",
            "detail": "Funnel ratios and deal tracking look reasonably healthy against the thresholds this "
                      "dashboard checks. Keep monitoring the rolling 4-week trend."
        })

    # Tag every suggestion so the dashboard can merge live HubSpot suggestions with
    # separately-generated GA4 suggestions without guessing based on text content.
    for item in s:
        item["source"] = "hubspot"

    return s


def main():
    print("Fetching lifecycle snapshot...")
    lifecycle_snapshot = fetch_lifecycle_snapshot()

    print("Fetching weekly Lead/MQL/SQL/Customer entries...")
    lead_weekly = fetch_weekly_entries("hs_v2_date_entered_lead", TRAILING_WEEKS)
    mql_weekly = fetch_weekly_entries("hs_v2_date_entered_marketingqualifiedlead", TRAILING_WEEKS)
    sql_weekly = fetch_weekly_entries("hs_v2_date_entered_salesqualifiedlead", TRAILING_WEEKS)
    customer_weekly = fetch_weekly_entries("hs_v2_date_entered_customer", TRAILING_WEEKS)

    print("Fetching SQL -> deal association rate...")
    sql_deal = fetch_sql_to_deal_rate(SQL_TO_DEAL_WINDOW_DAYS)

    print("Fetching source breakdown...")
    source_breakdown = fetch_source_breakdown(SOURCE_WINDOW_DAYS)

    print("Fetching deal summary...")
    deals = fetch_deal_summary()

    mql_anomalies = detect_anomaly_weeks(mql_weekly)
    sql_anomalies = detect_anomaly_weeks(sql_weekly)

    suggestions = build_suggestions(mql_weekly, sql_weekly, sql_deal, deals, source_breakdown,
                                     mql_anomalies, sql_anomalies)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "goals": {"weekly_mql": WEEKLY_MQL_GOAL, "weekly_sql": WEEKLY_SQL_GOAL},
        "lifecycle_snapshot": lifecycle_snapshot,
        "lead_weekly": lead_weekly,
        "mql_weekly": mql_weekly,
        "sql_weekly": sql_weekly,
        "customer_weekly": customer_weekly,
        "mql_avg_4wk": trailing_avg(mql_weekly, 4),
        "sql_avg_4wk": trailing_avg(sql_weekly, 4),
        "mql_anomaly_weeks": mql_anomalies,
        "sql_anomaly_weeks": sql_anomalies,
        "sql_to_deal": sql_deal,
        "source_breakdown": source_breakdown,
        "deals": deals,
        "suggestions": suggestions,
    }

    out_path = os.path.join(os.path.dirname(__file__), "docs", "data.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
