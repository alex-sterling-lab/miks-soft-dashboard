# miks-soft.com — Weekly Dashboard

Weekly report for Google Ads account **miks-soft.com** (CID 2821990435).
Data pulled directly from Google Ads API + GA4 Data API.

- **Live:** https://alex-sterling-lab.github.io/miks-soft-dashboard/
- **Period:** 22 июня 2026 — по настоящее (обновляется еженедельно)

## How data is collected

`pull_week.py START END --out data/week_START.json`

- Google Ads API (CID 2821990435): impressions, clicks, CTR, cost USD, CPC, form-submit conversions per campaign
- GA4 Data API (property 353999593): sessions, users, bounce rate, pages/session, avg session duration
- Yandex Direct / imedia CRM: пока не подключены (см. заметку в дашборде)

Затем `build.py` собирает все `week_*.json` в `data/all_weeks.json` и вшивает в `index.html`.

## Deploy

Any push to `master` -> auto-deployed to GH Pages (Pages -> Deploy from branch -> master / (root)).
