#!/usr/bin/env python3
"""miks-soft — сборщик понедельных данных для дашборда.

Тянет Google Ads (impressions/clicks/cost/CPC + form-submission conversions)
и GA4 (sessions/users/bounce/pages/duration/form_submit) за произвольную неделю.

Использование:
    ./pull_week.py 2026-06-22 2026-06-28
    ./pull_week.py 2026-06-22 2026-06-28 --out data.json
"""
import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials as OAuthCreds
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest,
)

# --- Google Ads ---
DEV_TOKEN = "V4_PMOHkPkJ0lTGFkkG_Eg"
ADS_CREDS = "/home/openclaw/.openclaw/workspace/google-ads-mcp/google-ads-credentials.json"
CID = "2821990435"  # miks-soft.com

# --- GA4 ---
GA4_KEY = "/home/openclaw/.config/google/service-account.json"
GA4_PROPERTY = "properties/353999593"  # miks-soft.com main


def usd_rub_rate(week_end):
    """USD→RUB по данным ЦБ РФ на последний рабочий день недели.
    ЦБ отдаёт архив по URL /archive/YYYY/MM/DD/daily_json.js — если суббота/воскресенье,
    отскакиваем к пятнице (курс ЦБ на выходные не меняется)."""
    d = date.fromisoformat(week_end)
    # ЦБ устанавливает курс на след. рабочий день; в выходные — тот же что в пятницу.
    for _ in range(7):
        url = f"https://www.cbr-xml-daily.ru/archive/{d.year}/{d.month:02d}/{d.day:02d}/daily_json.js"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.load(r)
            return {
                "rate": data["Valute"]["USD"]["Value"],
                "date": data["Date"][:10],
                "source": "CBR",
            }
        except Exception:
            d -= timedelta(days=1)
    # last resort — сегодняшний курс
    with urllib.request.urlopen("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10) as r:
        data = json.load(r)
    return {"rate": data["Valute"]["USD"]["Value"], "date": data["Date"][:10], "source": "CBR-current"}


def ads_client():
    c = json.load(open(ADS_CREDS))
    creds = OAuthCreds(
        None,
        refresh_token=c["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=c["client_id"],
        client_secret=c["client_secret"],
        scopes=["https://www.googleapis.com/auth/adwords"],
    )
    return GoogleAdsClient(credentials=creds, developer_token=DEV_TOKEN, use_proto_plus=True)


def fetch_ads_campaigns(start, end):
    cl = ads_client()
    svc = cl.get_service("GoogleAdsService")
    q = f"""
        SELECT
          campaign.name,
          campaign.advertising_channel_type,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.all_conversions
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    agg = defaultdict(lambda: {
        "type": "", "impressions": 0, "clicks": 0, "cost": 0.0,
        "conversions": 0.0, "all_conversions": 0.0,
    })
    for r in svc.search(customer_id=CID, query=q):
        name = r.campaign.name
        agg[name]["type"] = r.campaign.advertising_channel_type.name
        agg[name]["impressions"] += r.metrics.impressions
        agg[name]["clicks"] += r.metrics.clicks
        agg[name]["cost"] += r.metrics.cost_micros / 1_000_000
        agg[name]["conversions"] += r.metrics.conversions
        agg[name]["all_conversions"] += r.metrics.all_conversions
    # выкидываем кампании без показов — паузированные/архивные
    return {k: v for k, v in agg.items() if v["impressions"] > 0}


def fetch_ga4_by_source(start, end):
    """Забираем GA4: sessions/users/bounce/pages/duration + form_submit conversions,
    сгруппированные по sessionSource/sessionMedium. Так можно отделить google/cpc от
    yandex/organic и понять, какой доли метрик добился Google Ads."""
    creds = service_account.Credentials.from_service_account_file(
        GA4_KEY, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    client = BetaAnalyticsDataClient(credentials=creds)

    # 1) Traffic quality по source/medium
    req = RunReportRequest(
        property=GA4_PROPERTY,
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[
            Dimension(name="sessionSource"),
            Dimension(name="sessionMedium"),
        ],
        metrics=[
            Metric(name="sessions"),
            Metric(name="totalUsers"),
            Metric(name="bounceRate"),
            Metric(name="screenPageViewsPerSession"),
            Metric(name="averageSessionDuration"),
        ],
    )
    resp = client.run_report(req)
    rows = []
    for r in resp.rows:
        rows.append({
            "source": r.dimension_values[0].value,
            "medium": r.dimension_values[1].value,
            "sessions": int(r.metric_values[0].value),
            "users": int(r.metric_values[1].value),
            "bounce_rate": float(r.metric_values[2].value),
            "pages_per_session": float(r.metric_values[3].value),
            "avg_session_duration": float(r.metric_values[4].value),
        })

    # 2) Form submissions по source/medium
    form_req = RunReportRequest(
        property=GA4_PROPERTY,
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[
            Dimension(name="sessionSource"),
            Dimension(name="sessionMedium"),
            Dimension(name="eventName"),
        ],
        metrics=[Metric(name="eventCount")],
    )
    form_resp = client.run_report(form_req)
    forms = defaultdict(int)
    for r in form_resp.rows:
        event = r.dimension_values[2].value
        if event in ("form_submit", "generate_lead"):
            key = (r.dimension_values[0].value, r.dimension_values[1].value)
            forms[key] += int(r.metric_values[0].value)

    for row in rows:
        row["form_submits"] = forms.get((row["source"], row["medium"]), 0)

    return rows


def build_platform_view(ads_campaigns, ga4_rows, usd_rate):
    """Собираем в разрез 'платформа → кампания' с 20-колоночной структурой.

    Google Ads (импр/клики/CTR/расход/CPC + формы) — из Ads API.
    GA4-метрики сессий на каждую кампанию распределяются пропорционально долям
    google/cpc (это грубая аппроксимация; точнее только через utm_campaign)."""

    # GA4: агрегируем google/cpc в один блок и yandex/cpc отдельно
    ga4_by_platform = defaultdict(lambda: {
        "sessions": 0, "users": 0, "bounce_rate_num": 0.0, "bounce_rate_denom": 0,
        "pages_num": 0.0, "pages_denom": 0,
        "duration_num": 0.0, "duration_denom": 0,
        "form_submits": 0,
    })
    for r in ga4_rows:
        src = (r["source"], r["medium"])
        if src in [("google", "cpc"), ("google", "paid")]:
            key = "Google Ads"
        elif src in [("yandex", "cpc"), ("yandex", "paid"), ("direct.yandex.ru", "cpc")]:
            key = "Яндекс Директ"
        else:
            continue
        b = ga4_by_platform[key]
        b["sessions"] += r["sessions"]
        b["users"] += r["users"]
        # weighted mean = sum(metric * sessions) / sum(sessions)
        b["bounce_rate_num"] += r["bounce_rate"] * r["sessions"]
        b["bounce_rate_denom"] += r["sessions"]
        b["pages_num"] += r["pages_per_session"] * r["sessions"]
        b["pages_denom"] += r["sessions"]
        b["duration_num"] += r["avg_session_duration"] * r["sessions"]
        b["duration_denom"] += r["sessions"]
        b["form_submits"] += r["form_submits"]

    def ga4_totals(key):
        b = ga4_by_platform.get(key, {})
        s = b.get("sessions", 0)
        u = b.get("users", 0)
        return {
            "sessions": s,
            "users": u,
            "bounce_rate": (b["bounce_rate_num"] / b["bounce_rate_denom"]) if b.get("bounce_rate_denom") else None,
            "pages_per_session": (b["pages_num"] / b["pages_denom"]) if b.get("pages_denom") else None,
            "avg_session_duration": (b["duration_num"] / b["duration_denom"]) if b.get("duration_denom") else None,
            "form_submits": b.get("form_submits", 0),
        }

    # Google Ads campaigns — точные ad-метрики. GA4-метрики размажем пропорционально кликам.
    total_clicks = sum(v["clicks"] for v in ads_campaigns.values()) or 1
    ga = ga4_totals("Google Ads")

    google_rows = []
    for name, m in sorted(ads_campaigns.items()):
        share = m["clicks"] / total_clicks if total_clicks else 0
        # Form submits берём из Google Ads (метрика conversions привязана к GA4 form_submit)
        form_submits = int(round(m["conversions"]))
        cost_usd_val = round(m["cost"], 2)
        cost_rub_val = round(m["cost"] * usd_rate, 2)
        cpc_usd_val = round(m["cost"] / m["clicks"], 2) if m["clicks"] else None
        cpc_rub_val = round(m["cost"] * usd_rate / m["clicks"], 2) if m["clicks"] else None
        row = {
            "campaign": name,
            "type": m["type"],
            "impressions": m["impressions"],
            "clicks": m["clicks"],
            "ctr_pct": (m["clicks"] / m["impressions"] * 100) if m["impressions"] else None,
            "cost_rub": cost_rub_val,
            "cpc_rub": cpc_rub_val,
            "cost_usd": cost_usd_val,
            "cpc_usd": cpc_usd_val,
            "sessions": int(round(ga["sessions"] * share)) if ga["sessions"] else None,
            "users": int(round(ga["users"] * share)) if ga["users"] else None,
            "bounce_rate": ga["bounce_rate"],
            "pages_per_session": ga["pages_per_session"],
            "avg_session_duration": ga["avg_session_duration"],
            "form_submits": form_submits,
            "cost_per_form_usd": round(cost_usd_val / form_submits, 2) if form_submits else None,
            "crm_leads_total": None,
            "cost_per_crm_lead_usd": None,
            "crm_leads_converted": None,
            "cost_per_converted_lead_usd": None,
        }
        google_rows.append(row)

    # Google Ads Итого
    total_ga_row = {
        "campaign": "Итого",
        "type": "TOTAL",
        "impressions": sum(r["impressions"] for r in google_rows),
        "clicks": sum(r["clicks"] for r in google_rows),
        "cost_usd": round(sum(r["cost_usd"] for r in google_rows), 2),
        "cost_rub": round(sum(r["cost_rub"] for r in google_rows), 2),
    }
    total_ga_row["ctr_pct"] = (total_ga_row["clicks"] / total_ga_row["impressions"] * 100) if total_ga_row["impressions"] else None
    total_ga_row["cpc_usd"] = round(total_ga_row["cost_usd"] / total_ga_row["clicks"], 2) if total_ga_row["clicks"] else None
    total_ga_row["cpc_rub"] = round(total_ga_row["cost_rub"] / total_ga_row["clicks"], 2) if total_ga_row["clicks"] else None
    total_ga_row["sessions"] = ga["sessions"]
    total_ga_row["users"] = ga["users"]
    total_ga_row["bounce_rate"] = ga["bounce_rate"]
    total_ga_row["pages_per_session"] = ga["pages_per_session"]
    total_ga_row["avg_session_duration"] = ga["avg_session_duration"]
    # Форм по Google Ads = сумма conversions по всем кампаниям (это точнее чем GA4 form_submit по source)
    total_ga_row["form_submits"] = sum(r["form_submits"] for r in google_rows)
    total_ga_row["cost_per_form_usd"] = round(total_ga_row["cost_usd"] / total_ga_row["form_submits"], 2) if total_ga_row["form_submits"] else None
    total_ga_row["crm_leads_total"] = None
    total_ga_row["cost_per_crm_lead_usd"] = None
    total_ga_row["crm_leads_converted"] = None
    total_ga_row["cost_per_converted_lead_usd"] = None

    yandex_ga = ga4_totals("Яндекс Директ")
    yandex_note = None
    if not yandex_ga["sessions"]:
        yandex_note = "GA4 не видит трафика с yandex/cpc за неделю; вероятно кампании остановлены или не помечены utm."

    return {
        "platforms": [
            {
                "name": "Google Ads",
                "campaigns": google_rows,
                "total": total_ga_row,
            },
            {
                "name": "Яндекс Директ",
                "campaigns": [],
                "total": {
                    "campaign": "Итого",
                    "type": "TOTAL",
                    "note": yandex_note,
                    "sessions": yandex_ga["sessions"] or None,
                    "users": yandex_ga["users"] or None,
                    "bounce_rate": yandex_ga["bounce_rate"],
                    "pages_per_session": yandex_ga["pages_per_session"],
                    "avg_session_duration": yandex_ga["avg_session_duration"],
                    "form_submits": yandex_ga["form_submits"],
                },
            },
        ],
        "ga4_by_source": ga4_rows,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("start", help="YYYY-MM-DD")
    p.add_argument("end", help="YYYY-MM-DD")
    p.add_argument("--out", help="output json path")
    args = p.parse_args()

    print(f"Fetching Google Ads campaigns for {args.start} .. {args.end}", file=sys.stderr)
    ads = fetch_ads_campaigns(args.start, args.end)
    print(f"  -> {len(ads)} campaigns", file=sys.stderr)

    print(f"Fetching GA4 for {args.start} .. {args.end}", file=sys.stderr)
    ga4 = fetch_ga4_by_source(args.start, args.end)
    print(f"  -> {len(ga4)} source/medium rows", file=sys.stderr)

    print(f"Fetching USD→RUB rate for {args.end}", file=sys.stderr)
    rate_info = usd_rub_rate(args.end)
    print(f"  -> USD={rate_info['rate']:.4f} RUB (по {rate_info['date']}, {rate_info['source']})", file=sys.stderr)

    out = {
        "week": [args.start, args.end],
        "sources": {
            "google_ads": "CID 2821990435",
            "ga4": GA4_PROPERTY,
            "yandex_direct": "нет доступа",
            "crm": "нет доступа",
            "usd_rub": rate_info,
        },
        **build_platform_view(ads, ga4, rate_info["rate"]),
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"Written {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
