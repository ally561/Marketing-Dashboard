# Digital Brew Marketing Dashboard

A live funnel dashboard for Digital Brew: MQLs, SQLs, deal creation, win rate,
pipeline value, and closed-won revenue pulled straight from HubSpot, plus a
rules-based "marketing director suggestions" panel that flags real problems
in the data (SQLs with no deal, attribution gaps, bulk-import anomalies, etc).

It's a static site (no server, no login) so anyone on the team can open the
link and see current numbers. Data refreshes automatically once a week via
GitHub Actions, or on demand.

**`docs/data.json` currently contains a real snapshot pulled from your
HubSpot account on 2026-07-13**, so the dashboard works immediately. Follow
the steps below to make it refresh automatically.

## 1. Create the repo on GitHub

1. Create a new repo (private or public — private is fine, Pages works either way on a paid GitHub plan; public repos get free Pages on any plan).
2. From this folder, run:
   ```bash
   git init
   git add .
   git commit -m "Initial dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```

## 2. Turn on GitHub Pages

Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch: `main`, folder: `/docs` → Save.

GitHub will give you a URL like `https://<your-username>.github.io/<repo-name>/`.
Share that link with the team — no HubSpot login needed to view it.

## 3. Create a HubSpot Private App token

1. HubSpot → Settings → Integrations → **Private Apps** → Create a private app.
2. Scopes needed (read-only): `crm.objects.contacts.read`, `crm.objects.deals.read`.
3. Copy the generated access token.

## 4. Add the token as a GitHub secret

Repo → **Settings → Secrets and variables → Actions → New repository secret**
Name: `HUBSPOT_TOKEN`, value: the token from step 3.

## 5. Run it

- It runs automatically every Monday at 7am UTC (edit the cron schedule in
  `.github/workflows/update-dashboard.yml` if you want it more/less often).
- To run it right now: Repo → **Actions** → "Refresh marketing dashboard data" → **Run workflow**.
- Locally: `HUBSPOT_TOKEN=your-token python fetch_hubspot_data.py`

## What it tracks

- **Funnel snapshot** — current contact counts by lifecycle stage (Lead → MQL → SQL → Customer), year to date.
- **Weekly MQL/SQL trend** — last 12 weeks, plus a 4-week rolling average (weekly totals are noisy at Digital Brew's volume; the rolling average is the number worth reporting upward).
- **SQL → Deal rate** — the layer your ChatGPT conversation flagged as missing: what share of SQLs actually get a deal created in HubSpot.
- **Deal pipeline by stage** — count and dollar amount at each stage, plus win rate and closed-won revenue.
- **Source breakdown** — where recent MQLs originated (Paid Search, Organic, Referrals, Offline, etc.).
- **Suggestions panel** — rules-based checks against the data above (not a generic checklist): flags things like bulk-import weeks skewing your average, SQLs going stale with no deal, one traffic source dominating (often an attribution gap, not real performance), and thin paid-search volume.

## Editing goals or thresholds

Open `fetch_hubspot_data.py` and edit the constants near the top:

```python
WEEKLY_MQL_GOAL = 25
WEEKLY_SQL_GOAL = 10
SQL_TO_DEAL_WINDOW_DAYS = 120
SOURCE_WINDOW_DAYS = 45
```

The suggestion rules live in `build_suggestions()` in the same file — add,
remove, or adjust thresholds there as your definitions of MQL/SQL evolve.

## Files

```
fetch_hubspot_data.py          # pulls HubSpot data, computes metrics, writes docs/data.json
requirements.txt
.github/workflows/update-dashboard.yml   # weekly auto-refresh
docs/
  index.html                   # the dashboard itself (served by GitHub Pages)
  data.json                    # generated data (seeded with a real snapshot for now)
```
