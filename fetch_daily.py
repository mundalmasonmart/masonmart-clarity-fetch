"""
fetch_daily.py
--------------
Pulls today's data from the Clarity Data Export API and stores it in
clarity_history.sqlite. Run this daily via Task Scheduler.

Uses 4 of the allowed 10 API calls per day.
Stores: sessions, bot sessions, users, pages/session, by browser/device/source/URL.

SETUP: Create a .env file in this folder with:
  CLARITY_API_TOKEN=your-token-here
"""

import os
import sys
import json
import sqlite3
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("Run: pip install requests")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "clarity_history.sqlite")
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")
API_URL = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

FETCH_CALLS = [
    {"dimension1": "Browser"},
    {"dimension1": "URL"},
    {"dimension1": "Source", "dimension2": "Medium"},
    {"dimension1": "Device"},
]


def load_token():
    token = os.environ.get("CLARITY_API_TOKEN")
    if token:
        return token
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CLARITY_API_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_fetches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            fetch_date TEXT NOT NULL,
            dimensions TEXT,
            raw_json TEXT NOT NULL,
            record_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS daily_totals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL UNIQUE,
            total_sessions INTEGER DEFAULT 0,
            bot_sessions INTEGER DEFAULT 0,
            unique_users INTEGER DEFAULT 0,
            pages_per_session REAL DEFAULT 0
        );
    """)
    conn.commit()


def main():
    token = load_token()
    if not token:
        print(f"ERROR: No CLARITY_API_TOKEN found.")
        print(f"Create {ENV_PATH} with: CLARITY_API_TOKEN=your-token-here")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()

    # Check duplicate
    existing = conn.execute("SELECT COUNT(*) FROM api_fetches WHERE fetch_date=?", (today,)).fetchone()[0]
    if existing > 0:
        print(f"Already fetched for {today}. Skipping.")
        conn.close()
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    saved = 0
    total_sessions = 0
    total_bots = 0
    total_users = 0
    pps_sum = 0
    pps_weight = 0

    for call_params in FETCH_CALLS:
        params = {"numOfDays": 1}
        params.update(call_params)
        dims = ", ".join(f"{k}={v}" for k, v in call_params.items())
        print(f"  Fetching: {dims} ...")

        try:
            resp = requests.get(API_URL, headers=headers, params=params, timeout=30)
        except Exception as e:
            print(f"    [!] Error: {e}")
            continue

        if resp.status_code == 401:
            print("    [!] Token expired. Regenerate in Clarity: Settings > Data Export.")
            conn.close()
            sys.exit(1)
        if resp.status_code != 200:
            print(f"    [!] API {resp.status_code}: {resp.text[:200]}")
            continue

        data = resp.json()
        records = 0
        if isinstance(data, list):
            for block in data:
                for item in block.get("information", []):
                    records += 1
                    # Only the "Traffic" metric block carries these fields;
                    # other blocks (DeadClickCount, ScrollDepth, ...) don't,
                    # so skip them to avoid summing unrelated zeros/garbage.
                    if block.get("metricName") != "Traffic":
                        continue
                    s = int(item.get("totalSessionCount", 0))
                    b = int(item.get("totalBotSessionCount", 0))
                    u = int(item.get("distinctUserCount", 0))
                    p = float(item.get("pagesPerSessionPercentage", 0))
                    # Only count totals from first dimension set (avoid double counting)
                    if "Browser" in dims:
                        total_sessions += s
                        total_bots += b
                        total_users += u
                        pps_sum += s * p
                        pps_weight += s

        conn.execute(
            "INSERT INTO api_fetches (fetched_at,fetch_date,dimensions,raw_json,record_count) VALUES (?,?,?,?,?)",
            (now, today, dims, json.dumps(data), records))
        saved += 1
        print(f"    OK ({records} records)")

    # Store daily totals
    avg_pps = round(pps_sum / pps_weight, 2) if pps_weight > 0 else 0
    conn.execute("INSERT OR REPLACE INTO daily_totals (fetch_date,total_sessions,bot_sessions,unique_users,pages_per_session) VALUES (?,?,?,?,?)",
                 (today, total_sessions, total_bots, total_users, avg_pps))
    conn.commit()
    conn.close()

    print(f"\nDone. {saved} calls saved. Sessions: {total_sessions}, Bots: {total_bots}, Users: {total_users}")


if __name__ == "__main__":
    main()