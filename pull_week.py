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

# --- Клиентский Google Sheet (imedia CRM экспорт) ---
LEADS_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1JVrKnat6Ezg9A5wQVzFxfMbSnlImbWjFcQCz5vkcGoc/export?format=csv&gid=1092315723"
)
# Slug кампании из «Источник рекламы» → имя кампании в Google Ads
CAMPAIGN_SLUG_MAP = {
    "ak-search": "АК - Поиск (общий)(06/07 макс кликов 2$)",
    "ak-tier-1-2": "АК - PM  - Tier 1,2",
}

# Комментарий по неделям — из наших weekly-отчётов (наши работы над кампаниями)
COMMENTS_BY_WEEK = {
    ("2026-06-22", "АК - PM  - Tier 1,2"): "Исключили YouTube-плейсменты (29.06); PMax обучается с чистого трафика.",
    ("2026-06-29", "АК - PM  - Tier 1,2"): "PMax дообучается после чистки YouTube; довели семантику для новой Search до 100 kw/группу.",
    ("2026-06-29", "АК - Поиск (общий)(06/07 макс кликов 2$)"): "Создали новую поисковую с 6 группами (01.07), MaxConv $2 макс. клик; активна с 30.06.",
    ("2026-07-06", "АК - PM  - Tier 1,2"): "Аудит ассетов (07.07): дозаполнили headlines PMax «Тех. поддержка» 10→15. LOGO/BUSINESS_NAME на уровне кампании.",
    ("2026-07-06", "АК - Поиск (общий)(06/07 макс кликов 2$)"): "Добили семантику до 120 kw/группу (06.07, 720 kw всего). Первые 2 конверсии — «Сайт под ключ» и «Лендинг».",
}
MONTHS_RU = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
             "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}


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


def fetch_crm_leads(start, end):
    """Тянет клиентский лист «Лиды» (imedia CRM экспорт), фильтрует по неделе
    и Google-источнику, распределяет по кампаниям.

    Классификация:
      Все лиды из CRM     = все статусы кроме «Мусорная заявка» (спам)
      Сконв. лиды         = статусы, начинающиеся с «Сконвертирован» (дошли до сделки)"""
    import csv
    import re

    with urllib.request.urlopen(LEADS_CSV_URL, timeout=15) as r:
        text = r.read().decode("utf-8")

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    by_campaign = defaultdict(lambda: {"total": 0, "converted": 0})
    unmapped = []
    for row in csv.reader(text.splitlines()):
        if len(row) < 10 or row[0] == "Дата":
            continue
        m = re.match(r"(\d+) (\w+) (\d+) (\d+):(\d+)", row[0])
        if not m:
            continue
        d, mo, y, h, mi = m.groups()
        try:
            dt = date(int(y), MONTHS_RU[mo], int(d))
        except (KeyError, ValueError):
            continue
        if dt < start_d or dt > end_d:
            continue
        status = row[3].strip()
        if status == "Мусорная заявка":
            continue
        src = row[9].lower().strip()
        if "google" not in src:
            continue
        # slug кампании — второй/третий сегмент. Форматы:
        #   "cpc - google - ak-search - kw"
        #   "pm - google - ak-tier-1-2"
        #   "google - poisk-eur - kw"
        tokens = [t.strip() for t in src.split(" - ")]
        slug = None
        for t in tokens[1:]:
            if t == "google":
                continue
            if t in CAMPAIGN_SLUG_MAP:
                slug = t
                break
            # запомнить первый непустой не-brand токен как fallback slug
            if slug is None and t and t != "google":
                slug = t
        camp_name = CAMPAIGN_SLUG_MAP.get(slug)
        if camp_name is None:
            unmapped.append((row[0], slug, src[:60]))
            camp_name = f"(unmapped: {slug or 'unknown'})"
        by_campaign[camp_name]["total"] += 1
        if status.startswith("Сконвертирован"):
            by_campaign[camp_name]["converted"] += 1
    return dict(by_campaign), unmapped


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


def fetch_ga4_by_campaign(start, end):
    """Забираем GA4 в разрезе Google Ads campaign name.
    GA4 имеет встроенную dimension googleAdsCampaignName — сессии автоматически
    привязаны к Google Ads (через gclid), это точнее чем source/medium + share по кликам."""
    creds = service_account.Credentials.from_service_account_file(
        GA4_KEY, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    client = BetaAnalyticsDataClient(credentials=creds)

    req = RunReportRequest(
        property=GA4_PROPERTY,
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name="sessionGoogleAdsCampaignName")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="totalUsers"),
            Metric(name="bounceRate"),
            Metric(name="screenPageViewsPerSession"),
            Metric(name="averageSessionDuration"),
        ],
    )
    resp = client.run_report(req)
    by_camp = {}
    for r in resp.rows:
        name = r.dimension_values[0].value
        if not name or name in ("(not set)", "(other)"):
            continue
        by_camp[name] = {
            "sessions": int(r.metric_values[0].value),
            "users": int(r.metric_values[1].value),
            "bounce_rate": float(r.metric_values[2].value),
            "pages_per_session": float(r.metric_values[3].value),
            "avg_session_duration": float(r.metric_values[4].value),
        }
    return by_camp


def build_platform_view(ads_campaigns, ga4_rows, ga4_by_campaign, usd_rate, crm_by_campaign, week_start):
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
        # Per-campaign GA4 (точные, через googleAdsCampaignName). Если нет — фолбэк на share.
        cga = ga4_by_campaign.get(name)
        if cga and cga["sessions"] > 0:
            sessions_val = cga["sessions"]
            users_val = cga["users"]
            bounce_val = cga["bounce_rate"]
            pages_val = cga["pages_per_session"]
            dur_val = cga["avg_session_duration"]
        else:
            sessions_val = int(round(ga["sessions"] * share)) if ga["sessions"] else None
            users_val = int(round(ga["users"] * share)) if ga["users"] else None
            bounce_val = ga["bounce_rate"]
            pages_val = ga["pages_per_session"]
            dur_val = ga["avg_session_duration"]
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
            "sessions": sessions_val,
            "users": users_val,
            "bounce_rate": bounce_val,
            "pages_per_session": pages_val,
            "avg_session_duration": dur_val,
            "form_submits": form_submits,
            "cost_per_form_usd": round(cost_usd_val / form_submits, 2) if form_submits else None,
        }
        crm = crm_by_campaign.get(name, {"total": 0, "converted": 0})
        row["crm_leads_total"] = crm["total"]
        row["cost_per_crm_lead_usd"] = round(cost_usd_val / crm["total"], 2) if crm["total"] else None
        row["crm_leads_converted"] = crm["converted"]
        row["cost_per_converted_lead_usd"] = round(cost_usd_val / crm["converted"], 2) if crm["converted"] else None
        row["comment"] = COMMENTS_BY_WEEK.get((week_start, name), "")
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
    total_ga_row["crm_leads_total"] = sum(r["crm_leads_total"] for r in google_rows)
    total_ga_row["cost_per_crm_lead_usd"] = round(total_ga_row["cost_usd"] / total_ga_row["crm_leads_total"], 2) if total_ga_row["crm_leads_total"] else None
    total_ga_row["crm_leads_converted"] = sum(r["crm_leads_converted"] for r in google_rows)
    total_ga_row["cost_per_converted_lead_usd"] = round(total_ga_row["cost_usd"] / total_ga_row["crm_leads_converted"], 2) if total_ga_row["crm_leads_converted"] else None

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

    print(f"Fetching GA4 by campaign for {args.start} .. {args.end}", file=sys.stderr)
    ga4_camp = fetch_ga4_by_campaign(args.start, args.end)
    print(f"  -> {len(ga4_camp)} campaign rows: {list(ga4_camp)[:5]}", file=sys.stderr)

    print(f"Fetching USD→RUB rate for {args.end}", file=sys.stderr)
    rate_info = usd_rub_rate(args.end)
    print(f"  -> USD={rate_info['rate']:.4f} RUB (по {rate_info['date']}, {rate_info['source']})", file=sys.stderr)

    print(f"Fetching CRM leads for {args.start} .. {args.end}", file=sys.stderr)
    crm, unmapped = fetch_crm_leads(args.start, args.end)
    total_leads = sum(v["total"] for v in crm.values())
    total_conv = sum(v["converted"] for v in crm.values())
    print(f"  -> {total_leads} leads ({total_conv} converted) across {len(crm)} campaigns", file=sys.stderr)
    if unmapped:
        print(f"  -> WARN: {len(unmapped)} unmapped rows: {unmapped[:3]}", file=sys.stderr)

    out = {
        "week": [args.start, args.end],
        "sources": {
            "google_ads": "CID 2821990435",
            "ga4": GA4_PROPERTY,
            "yandex_direct": "нет доступа",
            "crm_sheet": "gid=1092315723 (клиентский лист «Лиды»)",
            "usd_rub": rate_info,
        },
        **build_platform_view(ads, ga4, ga4_camp, rate_info["rate"], crm, args.start),
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"Written {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
