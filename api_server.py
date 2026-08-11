#!/usr/bin/env python3
"""miks-soft dashboard — общее хранилище ручных значений (лиды и сделки).

Дашборд лежит на GitHub Pages статикой, поэтому вписанные числа раньше жили в
localStorage браузера — у каждого свои. Этот сервис даёт им одно место хранения,
чтобы значение видели все, кто открыл ссылку.

Значение адресуется парой (period, platform), например ("2026-07", "Google Ads").

  GET  /values                     -> {"2026-07::Google Ads": {"leads": 12, "deals": 3}}
  POST /values                     -> {"period": ..., "platform": ..., "leads": 12}
                                      можно слать любое подмножество полей
Хранилище — SQLite, вне рабочего дерева, чтобы не попасть в git и в GH Pages.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, g, jsonify, request

DB = Path(os.getenv("MIKS_DB", "/home/openclaw/.local/share/miks-dashboard/manual.db"))
# Токен виден в исходнике страницы — он отсекает случайных ботов, но не человека,
# открывшего DevTools. Для ручных счётчиков лидов этого достаточно; если понадобится
# настоящая защита — закрывать basic-auth'ом на nginx.
TOKEN = os.getenv("MIKS_TOKEN", "miks-2026-lead-entry")
ORIGINS = {
    "https://alex-sterling-lab.github.io",
    "https://sterling.company",
    "http://localhost:8000",
}
FIELDS = ("leads", "deals")

app = Flask(__name__)


def db() -> sqlite3.Connection:
    if "db" not in g:
        DB.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB)
        g.db.execute("""
            CREATE TABLE IF NOT EXISTS manual_values (
                period   TEXT NOT NULL,
                platform TEXT NOT NULL,
                leads    INTEGER,
                deals    INTEGER,
                updated  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (period, platform)
            )
        """)
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin", "")
    if origin in ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Token"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@app.route("/values", methods=["OPTIONS"])
def preflight():
    return ("", 204)


@app.get("/values")
def get_values():
    rows = db().execute(
        "SELECT period, platform, leads, deals FROM manual_values").fetchall()
    return jsonify({
        f"{p}::{pl}": {"leads": leads, "deals": deals}
        for p, pl, leads, deals in rows
    })


@app.post("/values")
def set_value():
    if request.headers.get("X-Token") != TOKEN:
        return jsonify({"error": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    period = str(body.get("period", "")).strip()
    platform = str(body.get("platform", "")).strip()
    if not period or not platform or len(period) > 32 or len(platform) > 64:
        return jsonify({"error": "period and platform required"}), 400

    updates = {}
    for f in FIELDS:
        if f not in body:
            continue
        v = body[f]
        if v in (None, ""):
            updates[f] = None
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            return jsonify({"error": f"{f} must be an integer"}), 400
        if not 0 <= n <= 1_000_000:
            return jsonify({"error": f"{f} out of range"}), 400
        updates[f] = n

    if not updates:
        return jsonify({"error": "nothing to update"}), 400

    conn = db()
    conn.execute(
        "INSERT INTO manual_values (period, platform) VALUES (?, ?) "
        "ON CONFLICT (period, platform) DO NOTHING", (period, platform))
    sets = ", ".join(f"{f} = ?" for f in updates)
    conn.execute(
        f"UPDATE manual_values SET {sets}, updated = datetime('now') "
        "WHERE period = ? AND platform = ?",
        (*updates.values(), period, platform))
    conn.commit()

    row = conn.execute(
        "SELECT leads, deals FROM manual_values WHERE period = ? AND platform = ?",
        (period, platform)).fetchone()
    return jsonify({"period": period, "platform": platform,
                    "leads": row[0], "deals": row[1]})


@app.get("/health")
def health():
    n = db().execute("SELECT COUNT(*) FROM manual_values").fetchone()[0]
    return jsonify({"ok": True, "rows": n, "db": str(DB)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8795")))
