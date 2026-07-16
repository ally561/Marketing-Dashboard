/**
 * Digital Brew Marketing Dashboard — live HubSpot proxy (Cloudflare Worker).
 *
 * This is a JS port of fetch_hubspot_data.py, designed to run on every page
 * load instead of once a week. It keeps your HubSpot token secret (set as a
 * Cloudflare Worker "secret", never committed to the repo or sent to the
 * browser) and returns the same JSON shape the dashboard already expects.
 *
 * Deploy via the Cloudflare dashboard (no CLI needed) — see README.md,
 * section "Live HubSpot data via Cloudflare Worker".
 *
 * Responses are cached at Cloudflare's edge for CACHE_SECONDS so a burst of
 * page loads doesn't hammer HubSpot's API or its rate limits — the tradeoff
 * for "live" is "at most CACHE_SECONDS old", which is the practical
 * definition of real-time for a dashboard like this.
 */

const CACHE_SECONDS = 60;

// ---- Config: keep in sync with fetch_hubspot_data.py ----------------------
const WEEKLY_MQL_GOAL = 25;
const WEEKLY_SQL_GOAL = 10;
const TRAILING_WEEKS = 53;
const SQL_TO_DEAL_WINDOW_DAYS = 120;
const SOURCE_WINDOW_DAYS = 45;

const LIFECYCLE_LABELS = {
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
};

const SOURCE_LABELS = {
  ORGANIC_SEARCH: "Organic Search",
  PAID_SEARCH: "Paid Search",
  EMAIL_MARKETING: "Email Marketing",
  SOCIAL_MEDIA: "Organic Social",
  REFERRALS: "Referrals",
  OTHER_CAMPAIGNS: "Other Campaigns",
  DIRECT_TRAFFIC: "Direct Traffic",
  OFFLINE: "Offline Sources",
  PAID_SOCIAL: "Paid Social",
  AI_REFERRALS: "AI Referrals",
};

const DEAL_STAGE_LABELS = {
  "1065773825": "Need Quote",
  "1065773826": "Quote Ready",
  "1065824343": "Quote Presented",
  decisionmakerboughtin: "Decision Maker Bought-In",
  "1065824354": "Agreement Sent",
  "1133742776": "Maybe Later",
  closedwon: "Closed Won",
  closedlost: "Closed Lost",
};

const BASE_URL = "https://api.hubapi.com";

// ---- HTTP helpers -----------------------------------------------------
async function hsSearch(token, objectType, filters, properties, limit = 200, maxPages = 50) {
  let results = [];
  let after = null;
  for (let i = 0; i < maxPages; i++) {
    const body = { filterGroups: filters.length ? [{ filters }] : [], properties, limit };
    if (after) body.after = after;
    const resp = await fetch(`${BASE_URL}/crm/v3/objects/${objectType}/search`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`HubSpot search ${objectType} failed: ${resp.status} ${await resp.text()}`);
    const data = await resp.json();
    results = results.concat(data.results || []);
    const nextAfter = data.paging && data.paging.next && data.paging.next.after;
    if (!nextAfter) break;
    after = nextAfter;
  }
  return results;
}

async function hsSearchTotal(token, objectType, filters) {
  const resp = await fetch(`${BASE_URL}/crm/v3/objects/${objectType}/search`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ filterGroups: [{ filters }], limit: 1 }),
  });
  if (!resp.ok) throw new Error(`HubSpot search-total ${objectType} failed: ${resp.status}`);
  const data = await resp.json();
  return data.total || 0;
}

// ---- Date helpers (mirrors fetch_hubspot_data.py) ----------------------
function isoDate(d) {
  return d.toISOString().slice(0, 10) + "T00:00:00Z";
}

function weekStart(d) {
  const day = (d.getUTCDay() + 6) % 7; // Monday = 0
  const ws = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  ws.setUTCDate(ws.getUTCDate() - day);
  return ws;
}

function weekKey(d) {
  return d.toISOString().slice(0, 10);
}

function bucketWeekly(dateStrings, weeksBack) {
  const now = new Date();
  const earliest = weekStart(new Date(now.getTime() - weeksBack * 7 * 86400000));
  const buckets = new Map();
  for (const ds of dateStrings) {
    if (!ds) continue;
    const wk = weekKey(weekStart(new Date(ds)));
    buckets.set(wk, (buckets.get(wk) || 0) + 1);
  }
  const ordered = [];
  let cur = new Date(earliest);
  const todayWeekKey = weekKey(weekStart(now));
  while (weekKey(cur) <= todayWeekKey) {
    const key = weekKey(cur);
    ordered.push({ week: key, count: buckets.get(key) || 0, partial: key === todayWeekKey });
    cur = new Date(cur.getTime() + 7 * 86400000);
  }
  return ordered;
}

function bucketWeeklyAmount(rows, weeksBack) {
  const now = new Date();
  const earliest = weekStart(new Date(now.getTime() - weeksBack * 7 * 86400000));
  const buckets = new Map();
  for (const r of rows) {
    if (!r.date) continue;
    const wk = weekKey(weekStart(new Date(r.date)));
    const b = buckets.get(wk) || { count: 0, amount: 0 };
    b.count += 1;
    b.amount += r.amount || 0;
    buckets.set(wk, b);
  }
  const ordered = [];
  let cur = new Date(earliest);
  const todayWeekKey = weekKey(weekStart(now));
  while (weekKey(cur) <= todayWeekKey) {
    const key = weekKey(cur);
    const b = buckets.get(key) || { count: 0, amount: 0 };
    ordered.push({ week: key, count: b.count, amount: Math.round(b.amount * 100) / 100, partial: key === todayWeekKey });
    cur = new Date(cur.getTime() + 7 * 86400000);
  }
  return ordered;
}

function trailingAvg(weekly, n, excludePartial = true) {
  const vals = weekly.filter((w) => !(excludePartial && w.partial)).map((w) => w.count);
  const last = vals.slice(-n);
  if (!last.length) return 0;
  return Math.round((last.reduce((a, b) => a + b, 0) / last.length) * 10) / 10;
}

function median(arr) {
  const s = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function detectAnomalyWeeks(weekly, factor = 2.5) {
  const counts = weekly.map((w) => w.count).filter((c) => c > 0);
  if (counts.length < 4) return [];
  const med = median(counts);
  const flagged = [];
  for (const w of weekly) {
    if (med > 0 && w.count > med * factor && w.count > 20) flagged.push(w.week);
  }
  return flagged;
}

// ---- Data fetchers (mirrors fetch_hubspot_data.py) ---------------------
async function fetchLifecycleSnapshot(token) {
  const yearStart = isoDate(new Date(Date.UTC(new Date().getUTCFullYear(), 0, 1)));
  const snapshot = {};
  for (const [stage, label] of Object.entries(LIFECYCLE_LABELS)) {
    const filters = [
      { propertyName: "lifecyclestage", operator: "EQ", value: stage },
      { propertyName: "createdate", operator: "GTE", value: yearStart },
    ];
    snapshot[label] = await hsSearchTotal(token, "contacts", filters);
  }
  return snapshot;
}

async function fetchWeeklyEntries(token, dateProperty, weeksBack) {
  const cutoff = isoDate(new Date(Date.now() - (weeksBack + 1) * 7 * 86400000));
  const filters = [{ propertyName: dateProperty, operator: "GTE", value: cutoff }];
  const rows = await hsSearch(token, "contacts", filters, [dateProperty]);
  const dates = rows.map((r) => r.properties[dateProperty]);
  return bucketWeekly(dates, weeksBack);
}

async function fetchSqlToDealRate(token, windowDays) {
  const cutoff = isoDate(new Date(Date.now() - windowDays * 86400000));
  const filters = [
    { propertyName: "lifecyclestage", operator: "EQ", value: "salesqualifiedlead" },
    { propertyName: "hs_v2_date_entered_salesqualifiedlead", operator: "GTE", value: cutoff },
  ];
  const rows = await hsSearch(token, "contacts", filters, ["num_associated_deals"]);
  const total = rows.length;
  const withDeal = rows.filter((r) => {
    const v = r.properties.num_associated_deals;
    return v && v !== "0";
  }).length;
  const rate = total ? Math.round((withDeal / total) * 1000) / 10 : null;
  return { sql_count: total, sql_with_deal: withDeal, rate_pct: rate, window_days: windowDays };
}

async function fetchSourceBreakdown(token, windowDays) {
  const cutoff = isoDate(new Date(Date.now() - windowDays * 86400000));
  const filters = [{ propertyName: "hs_v2_date_entered_marketingqualifiedlead", operator: "GTE", value: cutoff }];
  const rows = await hsSearch(token, "contacts", filters, ["hs_analytics_source"]);
  const counts = {};
  for (const r of rows) {
    const src = r.properties.hs_analytics_source || "UNKNOWN";
    const label = SOURCE_LABELS[src] || src;
    counts[label] = (counts[label] || 0) + 1;
  }
  return Object.fromEntries(Object.entries(counts).sort((a, b) => b[1] - a[1]));
}

async function fetchDealSummary(token) {
  const yearStart = isoDate(new Date(Date.UTC(new Date().getUTCFullYear(), 0, 1)));
  const filters = [{ propertyName: "createdate", operator: "GTE", value: yearStart }];
  const rows = await hsSearch(token, "deals", filters, [
    "dealstage", "amount_in_home_currency", "createdate",
    "hs_v2_date_entered_closedwon", "hs_v2_date_entered_closedlost",
  ]);

  const byStage = {};
  const createdDates = [];
  const wonRows = [];
  const lostRows = [];

  for (const r of rows) {
    const p = r.properties;
    const stage = DEAL_STAGE_LABELS[p.dealstage] || p.dealstage || "Unknown";
    const amt = parseFloat(p.amount_in_home_currency || "0") || 0;
    if (!byStage[stage]) byStage[stage] = { count: 0, amount: 0 };
    byStage[stage].count += 1;
    byStage[stage].amount += amt;
    createdDates.push(p.createdate);
    if (p.hs_v2_date_entered_closedwon) wonRows.push({ date: p.hs_v2_date_entered_closedwon, amount: amt });
    if (p.hs_v2_date_entered_closedlost) lostRows.push({ date: p.hs_v2_date_entered_closedlost, amount: amt });
  }

  const weeklyCreated = bucketWeekly(createdDates, TRAILING_WEEKS);
  const weeklyClosedWon = bucketWeeklyAmount(wonRows, TRAILING_WEEKS);
  const weeklyClosedLost = bucketWeeklyAmount(lostRows, TRAILING_WEEKS);

  const won = byStage["Closed Won"] || { count: 0, amount: 0 };
  const lost = byStage["Closed Lost"] || { count: 0, amount: 0 };
  const closedTotal = won.count + lost.count;
  const winRate = closedTotal ? Math.round((won.count / closedTotal) * 1000) / 10 : null;
  const openPipeline = Object.entries(byStage)
    .filter(([k]) => k !== "Closed Won" && k !== "Closed Lost")
    .reduce((a, [, v]) => a + v.amount, 0);

  const byStageOut = {};
  for (const [k, v] of Object.entries(byStage)) {
    byStageOut[k] = { count: v.count, amount: Math.round(v.amount * 100) / 100 };
  }

  return {
    by_stage: byStageOut,
    weekly_created: weeklyCreated,
    weekly_closed_won: weeklyClosedWon,
    weekly_closed_lost: weeklyClosedLost,
    closed_won_count: won.count,
    closed_won_revenue: Math.round(won.amount * 100) / 100,
    closed_lost_count: lost.count,
    win_rate_pct: winRate,
    open_pipeline_value: Math.round(openPipeline * 100) / 100,
  };
}

function buildSuggestions(mqlWeekly, sqlWeekly, sqlDeal, deals, sourceBreakdown, mqlAnomalies, sqlAnomalies) {
  const s = [];
  const mqlAvg4 = trailingAvg(mqlWeekly, 4);
  const sqlAvg4 = trailingAvg(sqlWeekly, 4);

  if (mqlAvg4 && mqlAvg4 < WEEKLY_MQL_GOAL * 0.7) {
    const gap = Math.round(((WEEKLY_MQL_GOAL - mqlAvg4) / Math.max(mqlAvg4, 1)) * 100);
    s.push({
      severity: "high",
      title: "Weekly MQL goal is well above current capacity",
      detail: `Trailing 4-week average is ${mqlAvg4} MQLs/week vs. a goal of ${WEEKLY_MQL_GOAL} (a ${gap}% gap). Treat this as a capacity/investment conversation with leadership, and report progress on a rolling 4-week basis rather than judging single weeks.`,
    });
  }
  if (sqlAvg4 && sqlAvg4 < WEEKLY_SQL_GOAL * 0.7) {
    const gap = Math.round(((WEEKLY_SQL_GOAL - sqlAvg4) / Math.max(sqlAvg4, 1)) * 100);
    s.push({
      severity: "high",
      title: "Weekly SQL goal is well above current capacity",
      detail: `Trailing 4-week average is ${sqlAvg4} SQLs/week vs. a goal of ${WEEKLY_SQL_GOAL} (a ${gap}% gap). Consider proposing an interim target (e.g. 60% of goal) while you fix the constraints below.`,
    });
  }

  if (mqlAnomalies.length || sqlAnomalies.length) {
    const weeks = [...new Set([...mqlAnomalies, ...sqlAnomalies])].sort();
    s.push({
      severity: "medium",
      title: "One or more weeks look like data anomalies, not organic funnel activity",
      detail: `Week(s) starting ${weeks.join(", ")} show MQL/SQL counts far above the trailing median — check for bulk imports, list re-syncs, or lifecycle-stage backfills. Left in, these spikes make your real weekly trend look better (or worse) than it is.`,
    });
  }

  if (sqlDeal.rate_pct !== null && sqlDeal.rate_pct < 70) {
    s.push({
      severity: "high",
      title: "A meaningful share of SQLs never get a deal created",
      detail: `Only ${sqlDeal.sql_with_deal} of ${sqlDeal.sql_count} SQLs (${sqlDeal.rate_pct}%) from the last ${sqlDeal.window_days} days have an associated deal. Add a workflow that flags SQLs with no deal after 14 days — this is often a bigger lever than generating more top-of-funnel leads.`,
    });
  }

  if (deals.win_rate_pct !== null && deals.win_rate_pct < 50) {
    s.push({
      severity: "medium",
      title: "Win rate on closed deals is below 50%",
      detail: `${deals.closed_won_count} won vs. ${deals.closed_lost_count} lost (${deals.win_rate_pct}% win rate). Pull the last 20-30 closed-lost deals and classify by reason (timing, budget, competitor, no response) before spending more on acquisition — a qualification or sales-process fix may be worth more than new leads.`,
    });
  }

  const totalSrc = Object.values(sourceBreakdown).reduce((a, b) => a + b, 0) || 1;
  const entries = Object.entries(sourceBreakdown);
  if (entries.length) {
    const [topSource, topCount] = entries[0];
    if (topCount / totalSrc > 0.5) {
      s.push({
        severity: topSource === "Offline Sources" || topSource === "Direct Traffic" ? "medium" : "low",
        title: `'${topSource}' accounts for over half of recent MQLs`,
        detail: `${topSource} produced ${topCount} of ${totalSrc} MQLs (${Math.round((topCount / totalSrc) * 100)}%) in the last window. If that's 'Offline Sources' or 'Direct Traffic', it usually means attribution is incomplete rather than that channel actually performing best — worth auditing tracking before reallocating budget.`,
      });
    }
  }

  const paid = sourceBreakdown["Paid Search"] || 0;
  if (totalSrc && paid / totalSrc < 0.15) {
    s.push({
      severity: "low",
      title: "Paid Search is a small share of recent MQLs",
      detail: `Paid Search produced only ${paid} of ${totalSrc} recent MQLs. If Google Ads spend hasn't dropped proportionally, cost per lead is likely climbing — review search terms and consider shifting Smart Bidding to optimize on MQL/SQL value rather than raw form fills.`,
    });
  }

  if (!s.length) {
    s.push({
      severity: "low",
      title: "No major red flags this run",
      detail: "Funnel ratios and deal tracking look reasonably healthy against the thresholds this dashboard checks. Keep monitoring the rolling 4-week trend.",
    });
  }

  for (const item of s) item.source = "hubspot";
  return s;
}

async function buildDashboardData(token) {
  const [lifecycleSnapshot, leadWeekly, mqlWeekly, sqlWeekly, customerWeekly, sqlDeal, sourceBreakdown, deals] =
    await Promise.all([
      fetchLifecycleSnapshot(token),
      fetchWeeklyEntries(token, "hs_v2_date_entered_lead", TRAILING_WEEKS),
      fetchWeeklyEntries(token, "hs_v2_date_entered_marketingqualifiedlead", TRAILING_WEEKS),
      fetchWeeklyEntries(token, "hs_v2_date_entered_salesqualifiedlead", TRAILING_WEEKS),
      fetchWeeklyEntries(token, "hs_v2_date_entered_customer", TRAILING_WEEKS),
      fetchSqlToDealRate(token, SQL_TO_DEAL_WINDOW_DAYS),
      fetchSourceBreakdown(token, SOURCE_WINDOW_DAYS),
      fetchDealSummary(token),
    ]);

  const mqlAnomalies = detectAnomalyWeeks(mqlWeekly);
  const sqlAnomalies = detectAnomalyWeeks(sqlWeekly);
  const suggestions = buildSuggestions(mqlWeekly, sqlWeekly, sqlDeal, deals, sourceBreakdown, mqlAnomalies, sqlAnomalies);

  return {
    generated_at: new Date().toISOString(),
    live: true,
    goals: { weekly_mql: WEEKLY_MQL_GOAL, weekly_sql: WEEKLY_SQL_GOAL },
    lifecycle_snapshot: lifecycleSnapshot,
    lead_weekly: leadWeekly,
    mql_weekly: mqlWeekly,
    sql_weekly: sqlWeekly,
    customer_weekly: customerWeekly,
    mql_avg_4wk: trailingAvg(mqlWeekly, 4),
    sql_avg_4wk: trailingAvg(sqlWeekly, 4),
    mql_anomaly_weeks: mqlAnomalies,
    sql_anomaly_weeks: sqlAnomalies,
    sql_to_deal: sqlDeal,
    source_breakdown: sourceBreakdown,
    deals,
    suggestions,
  };
}

// ---- Worker entrypoint --------------------------------------------------
export default {
  async fetch(request, env, ctx) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    const cache = caches.default;
    const cacheKey = new Request(request.url, request);
    const cached = await cache.match(cacheKey);
    if (cached) {
      const resp = new Response(cached.body, cached);
      for (const [k, v] of Object.entries(corsHeaders)) resp.headers.set(k, v);
      return resp;
    }

    if (!env.HUBSPOT_TOKEN) {
      return new Response(JSON.stringify({ error: "HUBSPOT_TOKEN secret is not configured on this Worker." }), {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      });
    }

    try {
      const data = await buildDashboardData(env.HUBSPOT_TOKEN);
      const body = JSON.stringify(data);
      const response = new Response(body, {
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": `public, max-age=${CACHE_SECONDS}`,
          ...corsHeaders,
        },
      });
      ctx.waitUntil(cache.put(cacheKey, response.clone()));
      return response;
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err && err.message ? err.message : err) }), {
        status: 502,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      });
    }
  },
};
