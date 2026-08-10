#!/usr/bin/env python3
"""miks-soft — сборщик помесячных данных для дашборда.

Тот же набор метрик, что и pull_week.py, но за календарный месяц.
Текущий месяц тянется по вчерашний день включительно и помечается partial=true.

Использование:
    ./pull_month.py 2026-07                       # -> stdout
    ./pull_month.py 2026-07 --out data/month_2026-07.json
"""
import argparse
import calendar
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from pull_week import (
    GA4_PROPERTY,
    build_platform_view,
    fetch_ads_campaigns,
    fetch_crm_leads,
    fetch_ga4_by_campaign,
    fetch_ga4_by_source,
    usd_rub_rate,
)


def month_bounds(ym):
    """'2026-07' -> ('2026-07-01', '2026-07-31', partial). Текущий месяц режем по вчера."""
    y, m = (int(x) for x in ym.split("-"))
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    yesterday = date.today() - timedelta(days=1)
    partial = end > yesterday
    if partial:
        end = yesterday
    return start.isoformat(), end.isoformat(), partial


def main():
    p = argparse.ArgumentParser()
    p.add_argument("month", help="YYYY-MM")
    p.add_argument("--out", help="output json path")
    args = p.parse_args()

    start, end, partial = month_bounds(args.month)
    if date.fromisoformat(end) < date.fromisoformat(start):
        raise SystemExit(f"{args.month}: месяц ещё не начался")
    print(f"Month {args.month}: {start} .. {end} (partial={partial})", file=sys.stderr)

    ads = fetch_ads_campaigns(start, end)
    print(f"  -> {len(ads)} campaigns", file=sys.stderr)
    ga4 = fetch_ga4_by_source(start, end)
    print(f"  -> {len(ga4)} source/medium rows", file=sys.stderr)
    ga4_camp = fetch_ga4_by_campaign(start, end)
    print(f"  -> {len(ga4_camp)} campaign rows", file=sys.stderr)
    rate_info = usd_rub_rate(end)
    print(f"  -> USD={rate_info['rate']:.4f} RUB ({rate_info['date']})", file=sys.stderr)
    crm, unmapped = fetch_crm_leads(start, end)
    print(f"  -> {sum(v['total'] for v in crm.values())} CRM leads", file=sys.stderr)
    if unmapped:
        print(f"  -> WARN: {len(unmapped)} unmapped rows: {unmapped[:3]}", file=sys.stderr)

    out = {
        "month": args.month,
        "period": [start, end],
        "partial": partial,
        "sources": {
            "google_ads": "CID 2821990435",
            "ga4": GA4_PROPERTY,
            "yandex_direct": "нет доступа",
            "crm_sheet": "gid=1092315723 (клиентский лист «Лиды»)",
            "usd_rub": rate_info,
        },
        **build_platform_view(ads, ga4, ga4_camp, rate_info["rate"], crm, start),
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"Written {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
