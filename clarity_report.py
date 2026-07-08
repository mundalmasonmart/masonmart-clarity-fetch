"""
clarity_report.py
-----------------
Reads a Microsoft Clarity dashboard CSV export, STORES the metrics in a
local SQLite database (so data accumulates over time), and generates
a full HTML report with charts, trend comparisons, and conclusions.

Every time you feed it a new CSV, the data gets added to the database.
When it generates a report, it compares the current period against the
previous period automatically — so you get arrows showing whether
things are improving or getting worse.

NO API KEYS. NO COST. NO INTERNET NEEDED (except to view charts in the
HTML output, which loads Chart.js from a CDN).

USAGE
-----
  python clarity_report.py path/to/Clarity_export.csv

  First run:  creates clarity_history.sqlite in the same folder as
              the script, imports the CSV, generates report.
  Later runs: imports the new CSV, compares against previous data,
              generates report with trend arrows.

The report is saved as an HTML file in the current folder.

REQUIREMENTS
------------
  Python 3.8+  (no pip installs — standard library only)
"""

import csv
import sys
import os
import re
import json
import sqlite3
from datetime import datetime, timedelta


DB_NAME = "clarity_history.sqlite"


# ─────────────────────────────────────────────
# 1. DATABASE SETUP & STORAGE
# ─────────────────────────────────────────────

def get_db_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_NAME)


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imported_at TEXT NOT NULL,
            date_range TEXT NOT NULL,
            date_start TEXT,
            date_end TEXT,
            csv_filename TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            total_sessions INTEGER,
            bot_sessions INTEGER,
            pages_per_session REAL,
            scroll_depth REAL,
            active_time INTEGER,
            total_time INTEGER,
            unique_users INTEGER,
            new_user_sessions INTEGER,
            returning_user_sessions INTEGER,
            perf_score REAL,
            lcp_seconds REAL,
            inp_ms REAL,
            cls_value REAL,
            js_error_sessions INTEGER,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sessions INTEGER,
            pct TEXT,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        CREATE TABLE IF NOT EXISTS browsers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sessions INTEGER,
            pct TEXT,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        CREATE TABLE IF NOT EXISTS top_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            sessions INTEGER,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        CREATE TABLE IF NOT EXISTS smart_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sessions INTEGER,
            pct TEXT,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        CREATE TABLE IF NOT EXISTS referrers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sessions INTEGER,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        CREATE TABLE IF NOT EXISTS js_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sessions INTEGER,
            pct TEXT,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );

        CREATE TABLE IF NOT EXISTS bot_traffic (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            bot_type TEXT NOT NULL,
            sessions INTEGER,
            FOREIGN KEY (import_id) REFERENCES imports(id)
        );
    """)
    conn.commit()


def parse_date_range(date_range_str):
    """Parse '07/05/2026 12:00 AM - 07/07/2026 11:59 PM' into start/end date strings."""
    parts = date_range_str.split(" - ")
    start = end = ""
    try:
        start = datetime.strptime(parts[0].strip(), "%m/%d/%Y %I:%M %p").strftime("%Y-%m-%d")
        end = datetime.strptime(parts[1].strip(), "%m/%d/%Y %I:%M %p").strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        pass
    return start, end


def store_data(conn, data, csv_filename):
    """Store parsed CSV data into the database. Returns the import_id."""
    date_start, date_end = parse_date_range(data["date_range"])

    # Check for duplicate import (same date range and filename)
    existing = conn.execute(
        "SELECT id FROM imports WHERE date_range = ? AND csv_filename = ?",
        (data["date_range"], csv_filename)
    ).fetchone()
    if existing:
        print(f"  [!] This CSV ({csv_filename}) with date range '{data['date_range']}' is already imported (id={existing[0]}). Skipping duplicate.")
        return existing[0]

    cur = conn.execute(
        "INSERT INTO imports (imported_at, date_range, date_start, date_end, csv_filename) VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), data["date_range"], date_start, date_end, csv_filename)
    )
    import_id = cur.lastrowid

    # Parse numeric performance values
    lcp_val = 0
    m = re.search(r"([\d.]+)", data["performance"]["lcp"])
    if m: lcp_val = float(m.group(1))
    inp_val = 0
    m = re.search(r"([\d.]+)", data["performance"]["inp"])
    if m: inp_val = float(m.group(1))
    cls_val = 0
    m = re.search(r"([\d.]+)", data["performance"]["cls"])
    if m: cls_val = float(m.group(1))

    conn.execute(
        "INSERT INTO snapshots (import_id, total_sessions, bot_sessions, pages_per_session, "
        "scroll_depth, active_time, total_time, unique_users, new_user_sessions, "
        "returning_user_sessions, perf_score, lcp_seconds, inp_ms, cls_value, js_error_sessions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (import_id, data["sessions"]["total"], data["sessions"]["bot"],
         data["pages_per_session"], data["scroll_depth"],
         data["active_time"], data["total_time"],
         data["users"]["unique"], data["users"]["new"], data["users"]["returning"],
         data["performance"]["score"], lcp_val, inp_val, cls_val,
         data["js_errors"]["total"])
    )

    for name, vals in data["insights"].items():
        conn.execute("INSERT INTO insights (import_id, name, sessions, pct) VALUES (?, ?, ?, ?)",
                     (import_id, name, vals["sessions"], vals["pct"]))

    for b in data["browsers"]:
        conn.execute("INSERT INTO browsers (import_id, name, sessions, pct) VALUES (?, ?, ?, ?)",
                     (import_id, b["name"], b["sessions"], b["pct"]))

    for p in data["top_pages"]:
        conn.execute("INSERT INTO top_pages (import_id, url, sessions) VALUES (?, ?, ?)",
                     (import_id, p["url"], p["sessions"]))

    for e in data["smart_events"]:
        conn.execute("INSERT INTO smart_events (import_id, name, sessions, pct) VALUES (?, ?, ?, ?)",
                     (import_id, e["name"], e["sessions"], e["pct"]))

    for r in data["referrers"]:
        conn.execute("INSERT INTO referrers (import_id, name, sessions) VALUES (?, ?, ?)",
                     (import_id, r["name"], r["sessions"]))

    for e in data["js_errors"]["errors"]:
        conn.execute("INSERT INTO js_errors (import_id, name, sessions, pct) VALUES (?, ?, ?, ?)",
                     (import_id, e["name"], e["sessions"], e["pct"]))

    for key, val in data["bot_traffic"].items():
        conn.execute("INSERT INTO bot_traffic (import_id, bot_type, sessions) VALUES (?, ?, ?)",
                     (import_id, key, val))

    conn.commit()
    return import_id


def load_previous_snapshot(conn, current_import_id):
    """Load the most recent snapshot BEFORE the current one for comparison."""
    row = conn.execute(
        "SELECT * FROM snapshots WHERE import_id < ? ORDER BY import_id DESC LIMIT 1",
        (current_import_id,)
    ).fetchone()
    if not row:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM snapshots LIMIT 0").description]
    return dict(zip(cols, row))


def load_history(conn, limit=30):
    """Load trend data from BOTH CSV snapshots and daily API fetches."""
    history = []

    # 1. CSV-based snapshots
    rows = conn.execute(
        "SELECT s.*, i.date_start, i.date_end, i.date_range "
        "FROM snapshots s JOIN imports i ON s.import_id = i.id "
        "ORDER BY i.date_start DESC LIMIT ?", (limit,)
    ).fetchall()
    cols = [d[0] for d in conn.execute(
        "SELECT s.*, i.date_start, i.date_end, i.date_range "
        "FROM snapshots s JOIN imports i ON s.import_id = i.id LIMIT 0"
    ).description]
    for r in rows:
        d = dict(zip(cols, r))
        d["_source"] = "csv"
        history.append(d)

    # 2. Daily API totals (from fetch_daily.py, if that table exists)
    try:
        api_rows = conn.execute(
            "SELECT fetch_date as date_start, total_sessions, bot_sessions, "
            "unique_users, pages_per_session FROM daily_totals "
            "ORDER BY fetch_date DESC LIMIT ?", (limit,)
        ).fetchall()
        api_cols = ["date_start", "total_sessions", "bot_sessions",
                    "unique_users", "pages_per_session"]
        for r in api_rows:
            d = dict(zip(api_cols, r))
            d["_source"] = "api"
            # Fill missing fields with 0 so trend charts don't break
            d.setdefault("perf_score", 0)
            d.setdefault("lcp_seconds", 0)
            d.setdefault("inp_ms", 0)
            history.append(d)
    except sqlite3.OperationalError:
        pass  # daily_totals table doesn't exist yet — fetch_daily.py hasn't run

    # Deduplicate: if same date exists in both CSV and API, prefer CSV
    seen_dates = set()
    deduped = []
    # Sort by date, CSV first (so CSV wins on duplicate dates)
    history.sort(key=lambda x: (x.get("date_start", ""), x.get("_source") == "api"))
    for h in history:
        dt = h.get("date_start", "")
        if dt not in seen_dates:
            seen_dates.add(dt)
            deduped.append(h)

    deduped.sort(key=lambda x: x.get("date_start", ""))
    return deduped[-limit:]


def count_imports(conn):
    row = conn.execute("SELECT COUNT(*) FROM imports").fetchone()
    return row[0] if row else 0


# ─────────────────────────────────────────────
# 2. PARSE THE CLARITY CSV
# ─────────────────────────────────────────────

def parse_clarity_csv(filepath):
    data = {
        "project_name": "", "date_range": "",
        "sessions": {"total": 0, "bot": 0},
        "pages_per_session": 0, "scroll_depth": 0,
        "active_time": 0, "total_time": 0,
        "users": {"unique": 0, "new": 0, "returning": 0},
        "insights": {}, "browsers": [], "top_pages": [],
        "smart_events": [], "referrers": [],
        "js_errors": {"total": 0, "errors": []},
        "performance": {"score": 0, "lcp": "", "inp": "", "cls": ""},
        "bot_traffic": {},
    }

    with open(filepath, "r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    current_section = None
    for row in rows:
        if not row or all(c.strip() == "" for c in row):
            continue
        if row[0] == "Project name":
            data["project_name"] = row[1] if len(row) > 1 else ""; continue
        if row[0] == "Date range":
            data["date_range"] = row[1] if len(row) > 1 else ""; continue
        if row[0] == "Metric":
            current_section = row[1].strip() if len(row) > 1 else None; continue

        if current_section == "Sessions":
            if "Total sessions" in str(row):
                data["sessions"]["total"] = int(row[2]) if len(row) > 2 else 0
            elif "Bot sessions" in str(row):
                data["sessions"]["bot"] = int(row[2]) if len(row) > 2 else 0
        elif current_section == "Pages per session":
            if "Average" in str(row):
                data["pages_per_session"] = round(float(row[2]), 1) if len(row) > 2 else 0
        elif current_section == "Scroll depth":
            if "Average" in str(row):
                data["scroll_depth"] = round(float(row[2]), 1) if len(row) > 2 else 0
        elif current_section == "Active time spent":
            if "Active time" in str(row):
                data["active_time"] = int(row[2]) if len(row) > 2 else 0
            elif "Total time" in str(row):
                data["total_time"] = int(row[2]) if len(row) > 2 else 0
        elif current_section == "Users overview":
            if "Unique users" in str(row):
                data["users"]["unique"] = int(row[2]) if len(row) > 2 else 0
            elif "new users" in str(row):
                data["users"]["new"] = int(row[2]) if len(row) > 2 else 0
            elif "returning" in str(row):
                data["users"]["returning"] = int(row[2]) if len(row) > 2 else 0
        elif current_section == "Insights":
            if len(row) >= 3 and row[1].strip() and row[1].strip() != "No. of sessions":
                data["insights"][row[1].strip()] = {
                    "sessions": int(row[2]) if row[2].strip().isdigit() else 0,
                    "pct": row[3].strip() if len(row) > 3 else "0%"}
        elif current_section == "Browsers":
            if len(row) >= 3 and row[1].strip() and row[1].strip() != "No. of sessions":
                data["browsers"].append({"name": row[1].strip(),
                    "sessions": int(row[2]) if row[2].strip().isdigit() else 0,
                    "pct": row[3].strip() if len(row) > 3 else ""})
        elif current_section == "Top pages":
            if len(row) >= 3 and row[1].strip() and row[1].strip() != "No. of sessions":
                data["top_pages"].append({"url": row[1].strip(),
                    "sessions": int(row[2]) if row[2].strip().isdigit() else 0})
        elif current_section == "Smart events":
            if len(row) >= 3 and row[1].strip() and row[1].strip() != "No. of sessions":
                data["smart_events"].append({"name": row[1].strip(),
                    "sessions": int(row[2]) if row[2].strip().isdigit() else 0,
                    "pct": row[3].strip() if len(row) > 3 else ""})
        elif current_section == "Referrer":
            if len(row) >= 3 and row[1].strip() and row[1].strip() != "No. of sessions":
                data["referrers"].append({"name": row[1].strip(),
                    "sessions": int(row[2]) if row[2].strip().isdigit() else 0})
        elif current_section == "JavaScript errors":
            if "total JavaScript errors" in str(row):
                data["js_errors"]["total"] = int(row[2]) if len(row) > 2 and row[2].strip().isdigit() else 0
            elif len(row) >= 3 and row[1].strip() and row[1].strip() not in ("Sessions with JavaScript errors", "", "No. of sessions"):
                data["js_errors"]["errors"].append({"name": row[1].strip(),
                    "sessions": int(row[2]) if row[2].strip().isdigit() else 0,
                    "pct": row[3].strip() if len(row) > 3 else ""})
        elif current_section == "Performance overview":
            if "Score" in str(row):
                data["performance"]["score"] = round(float(row[2]), 1) if len(row) > 2 else 0
            elif "LCP" in str(row): data["performance"]["lcp"] = row[2].strip() if len(row) > 2 else ""
            elif "INP" in str(row): data["performance"]["inp"] = row[2].strip() if len(row) > 2 else ""
            elif "CLS" in str(row): data["performance"]["cls"] = row[2].strip() if len(row) > 2 else ""

    for row in rows:
        if len(row) >= 2 and (row[0].strip().endswith("BotSessions") or row[0].strip().endswith("Sessions")):
            key = row[0].strip()
            if key != "__typename":
                data["bot_traffic"][key] = int(row[1]) if row[1].strip().isdigit() else 0

    return data


# ─────────────────────────────────────────────
# 3. TREND HELPERS
# ─────────────────────────────────────────────

def trend_arrow(current, previous, lower_is_better=False):
    """Return an HTML arrow + percentage change string."""
    if previous is None or previous == 0:
        return '<span style="color:#898781;">— new</span>'
    change = ((current - previous) / previous) * 100
    if abs(change) < 1:
        return '<span style="color:#898781;">→ flat</span>'
    if lower_is_better:
        color = "#1d9e75" if change < 0 else "#d85a30"
        arrow = "↓" if change < 0 else "↑"
    else:
        color = "#1d9e75" if change > 0 else "#d85a30"
        arrow = "↑" if change > 0 else "↓"
    return f'<span style="color:{color};">{arrow} {abs(round(change))}% vs prev</span>'


# ─────────────────────────────────────────────
# 4. CONCLUSIONS (rule-based)
# ─────────────────────────────────────────────

def generate_conclusions(d, prev):
    conclusions = []
    total = d["sessions"]["total"]
    if total == 0:
        return conclusions

    mobile_names = ["ChromeMobile", "MobileSafari", "GoogleApp", "InstagramApp",
                    "SamsungInternet", "AndroidBrowser", "UCBrowser", "OperaMobile",
                    "FacebookApp", "TwitterApp", "PinterestApp"]
    mobile_sessions = sum(b["sessions"] for b in d["browsers"] if b["name"] in mobile_names)
    mobile_pct = round(mobile_sessions / total * 100, 1)

    if mobile_pct > 55:
        conclusions.append(("device", "info",
            f"<strong>{mobile_pct}% of traffic is mobile.</strong> "
            "Your visitors are predominantly on phones. Every UX fix should be tested on mobile first."))

    qb = d["insights"].get("Quick back click", {})
    qb_sessions = qb.get("sessions", 0)
    qb_pct = round(qb_sessions / total * 100, 1)

    # Compare quick-back with previous period
    qb_trend = ""
    if prev:
        prev_qb_pct = 0
        prev_import = prev["import_id"]
        # We'd need to query insights — simplified: just use the snapshot-level comparison
        if prev.get("total_sessions", 0) > 0:
            qb_trend = " (compare with previous period manually in Clarity)"

    if qb_pct > 25:
        conclusions.append(("friction", "critical",
            f"<strong>Quick-back clicks at {qb_pct}% — #1 red flag.</strong> "
            f"{qb_sessions} of {total} sessions. People are landing and immediately retreating. "
            "Watch 5–10 session recordings filtered by quick-back behavior."))
    elif qb_pct > 15:
        conclusions.append(("friction", "warn",
            f"<strong>Quick-back clicks at {qb_pct}% — worth monitoring.</strong>"))

    rc = d["insights"].get("Rage clicks", {})
    if rc.get("sessions", 0) == 0:
        conclusions.append(("friction", "good",
            "<strong>Zero rage clicks — nothing is frustrating users.</strong> No broken buttons detected."))
    elif rc.get("sessions", 0) > 5:
        conclusions.append(("friction", "critical",
            f"<strong>Rage clicks in {rc['sessions']} sessions.</strong> Check recordings immediately."))

    dc = d["insights"].get("Dead click", {})
    if dc.get("sessions", 0) > 0:
        conclusions.append(("friction", "info",
            f"<strong>Dead clicks in {dc['sessions']} sessions.</strong> "
            "Elements being clicked that aren't interactive."))

    rq = next((e for e in d["smart_events"] if e["name"].lower() == "request quote"), None)
    pv = next((e for e in d["smart_events"] if e["name"].lower() == "product viewed"), None)
    atc = next((e for e in d["smart_events"] if e["name"].lower() == "add to cart"), None)
    rq_count = rq["sessions"] if rq else 0
    pv_count = pv["sessions"] if pv else 0
    atc_count = atc["sessions"] if atc else 0

    if rq_count <= 1 and pv_count > 5:
        conclusions.append(("funnel", "critical",
            f"<strong>Request Quote nearly invisible — only {rq_count} session(s).</strong> "
            f"{pv_count} viewed products, {atc_count} added to cart, but almost nobody requested a quote. "
            "Check visibility on mobile product pages."))

    if atc_count > 0:
        bc = next((e for e in d["smart_events"] if e["name"].lower() == "begin checkout"), None)
        bc_count = bc["sessions"] if bc else 0
        if bc_count > 0:
            retention = round(bc_count / atc_count * 100)
            level = "good" if retention > 50 else "warn"
            conclusions.append(("funnel", level,
                f"<strong>Cart-to-checkout retention: {retention}%.</strong> "
                f"{bc_count} of {atc_count} add-to-cart sessions began checkout."))

    oc = next((e for e in d["smart_events"] if e["name"].lower() == "outbound click"), None)
    if oc and oc["sessions"] > 3:
        conclusions.append(("funnel", "warn",
            f"<strong>Outbound clicks in {oc['sessions']} sessions.</strong> "
            "Are these WhatsApp (conversions) or people leaving? Check recordings."))

    lcp_val = 0
    m = re.search(r"([\d.]+)", d["performance"]["lcp"])
    if m: lcp_val = float(m.group(1))
    inp_val = 0
    m = re.search(r"([\d.]+)", d["performance"]["inp"])
    if m: inp_val = float(m.group(1))
    cls_val = 0
    m = re.search(r"([\d.]+)", d["performance"]["cls"])
    if m: cls_val = float(m.group(1))

    if lcp_val > 2.5:
        lcp_trend = ""
        if prev and prev.get("lcp_seconds", 0) > 0:
            diff = lcp_val - prev["lcp_seconds"]
            if abs(diff) > 0.1:
                lcp_trend = f" ({'↑ worse' if diff > 0 else '↓ improved'} by {abs(round(diff, 2))}s vs previous)"
        conclusions.append(("perf", "warn",
            f"<strong>LCP at {lcp_val}s exceeds Google's 2.5s threshold.{lcp_trend}</strong> "
            "Affects quick-back rate and Google Ads Quality Score."))
    elif lcp_val > 0:
        conclusions.append(("perf", "good", f"<strong>LCP at {lcp_val}s — within Google's good range.</strong>"))

    if inp_val > 200:
        conclusions.append(("perf", "warn", f"<strong>INP at {inp_val}ms exceeds 200ms threshold.</strong>"))
    if cls_val < 0.1:
        conclusions.append(("perf", "good", f"<strong>CLS at {cls_val} — excellent, no layout shift.</strong>"))

    ppc_fraud = d["bot_traffic"].get("ppcAdFraudBotSessions", 0)
    if ppc_fraud == 0:
        conclusions.append(("bot", "good",
            "<strong>Zero PPC ad fraud — your ad spend is clean.</strong>"))
    elif ppc_fraud > 0:
        conclusions.append(("bot", "critical",
            f"<strong>{ppc_fraud} PPC ad fraud sessions detected!</strong> Investigate immediately."))

    bot_total = d["sessions"]["bot"]
    if bot_total > total:
        conclusions.append(("bot", "info",
            f"<strong>Bots ({bot_total}) outnumber real visitors ({total}).</strong> "
            "Already excluded from your data — just noise."))

    if d["top_pages"]:
        top = d["top_pages"][0]
        top_pct = round(top["sessions"] / total * 100, 1)
        if top_pct > 40:
            page_label = top["url"].replace("https://www.masonmart.in", "") or "Homepage"
            conclusions.append(("pages", "info",
                f"<strong>{page_label} carries {top_pct}% of traffic.</strong> Highest-leverage page to optimize."))

    search_ev = next((e for e in d["smart_events"] if e["name"].lower() == "search"), None)
    if search_ev and search_ev["sessions"] <= 5 and total > 30:
        conclusions.append(("pages", "info",
            f"<strong>Site search usage is low ({search_ev['sessions']} sessions).</strong> "
            "Check if search bar is visible on mobile."))

    new_pct = round(d["users"]["new"] / total * 100, 1) if total else 0
    if new_pct > 80:
        conclusions.append(("users", "info",
            f"<strong>{new_pct}% are first-time visitors.</strong> "
            "Growth mode — but long-term B2B success needs returning customers."))

    return conclusions


def generate_priorities(d, conclusions):
    priorities = []
    total = d["sessions"]["total"]

    has_rq = any("Request Quote" in c[2] and c[1] == "critical" for c in conclusions)
    if has_rq:
        priorities.append(("p1", "Fix or promote Request Quote on mobile",
            "Almost nobody is using it. Check if the button is above the fold on mobile product pages."))

    has_qb = any("Quick-back" in c[2] for c in conclusions)
    if has_qb:
        qb = d["insights"].get("Quick back click", {})
        priorities.append(("p2", f"Investigate the {round(qb.get('sessions',0)/total*100)}% quick-back rate",
            "Watch session recordings. Focus on homepage and collection pages."))

    has_lcp = any("LCP" in c[2] and c[1] == "warn" for c in conclusions)
    if has_lcp:
        priorities.append(("p3", f"Improve page load speed (LCP: {d['performance']['lcp']} → target 2.5s)",
            "Check large images, heavy third-party scripts, PageFly components."))

    google_sessions = sum(r["sessions"] for r in d["referrers"] if "google" in r["name"].lower())
    if google_sessions > 10:
        priorities.append(("p4", "Separate Google Ads vs organic in Clarity",
            "Use UTM parameters to distinguish paid vs organic — critical for campaign decisions."))

    has_oc = any("Outbound" in c[2] for c in conclusions)
    if has_oc:
        priorities.append(("p5", "Check outbound-click sessions",
            "WhatsApp = conversion. Competitor link = lost traffic. Find out which."))

    if d["js_errors"]["total"] > 5:
        priorities.append(("p5", "Review JavaScript errors",
            f"{d['js_errors']['total']} sessions had errors. Check browser console on main pages."))

    if len(priorities) < 3:
        priorities.append(("p5", "Keep collecting data for trend analysis",
            "More data = better conclusions. Export and run this script weekly."))

    return priorities[:6]


# ─────────────────────────────────────────────
# 5. BUILD HTML (with trend support)
# ─────────────────────────────────────────────

def build_html(d, conclusions, priorities, prev, history, import_count):
    total = d["sessions"]["total"]
    bot_total = d["sessions"]["bot"]

    mobile_names = ["ChromeMobile", "MobileSafari", "GoogleApp", "InstagramApp",
                    "SamsungInternet", "AndroidBrowser", "UCBrowser", "OperaMobile"]
    mobile_sessions = sum(b["sessions"] for b in d["browsers"] if b["name"] in mobile_names)
    desktop_sessions = total - mobile_sessions
    mobile_pct = round(mobile_sessions / total * 100, 1) if total else 0

    qb = d["insights"].get("Quick back click", {})
    qb_pct = round(qb.get("sessions", 0) / total * 100, 1) if total else 0

    active_ratio = round(d["active_time"] / d["total_time"] * 100) if d["total_time"] else 0

    def short_url(url):
        return url.replace("https://www.masonmart.in", "").replace("https://masonmart.in", "") or "Homepage"

    # Trend arrows for KPIs
    sessions_trend = trend_arrow(total, prev["total_sessions"] if prev else None)
    pps_trend = trend_arrow(d["pages_per_session"], prev["pages_per_session"] if prev else None)
    scroll_trend = trend_arrow(d["scroll_depth"], prev["scroll_depth"] if prev else None)
    qb_trend = trend_arrow(qb.get("sessions", 0),
        None, lower_is_better=True)  # simplified — would need insight history for full comparison

    # Performance
    lcp_val = 0; m = re.search(r"([\d.]+)", d["performance"]["lcp"])
    if m: lcp_val = float(m.group(1))
    inp_val = 0; m = re.search(r"([\d.]+)", d["performance"]["inp"])
    if m: inp_val = float(m.group(1))
    cls_val = 0; m = re.search(r"([\d.]+)", d["performance"]["cls"])
    if m: cls_val = float(m.group(1))
    perf_score = d["performance"]["score"]

    lcp_color = "#1d9e75" if lcp_val <= 2.5 else "#d85a30"
    inp_color = "#1d9e75" if inp_val <= 200 else "#d85a30"
    cls_color = "#1d9e75" if cls_val < 0.1 else "#d85a30"
    score_color = "#1d9e75" if perf_score >= 90 else "#eda100" if perf_score >= 70 else "#e34948"

    lcp_trend = trend_arrow(lcp_val, prev["lcp_seconds"] if prev else None, lower_is_better=True)
    inp_trend = trend_arrow(inp_val, prev["inp_ms"] if prev else None, lower_is_better=True)
    perf_trend = trend_arrow(perf_score, prev["perf_score"] if prev else None)

    # Conclusion HTML helper
    def conclusion_html(level, text):
        css = {"critical": "critical", "warn": "warn", "good": "good"}.get(level, "")
        return f'<div class="conclusion {css}">{text}</div>'

    conclusions_by_section = {}
    for section, level, text in conclusions:
        conclusions_by_section.setdefault(section, []).append((level, text))

    def sec_conclusions(section):
        return "\n".join(conclusion_html(l, t) for l, t in conclusions_by_section.get(section, []))

    # Chart data
    browser_labels = json.dumps([b["name"] for b in d["browsers"]])
    browser_data = json.dumps([b["sessions"] for b in d["browsers"]])
    browser_colors = json.dumps(["#2a78d6","#3987e5","#1baf7a","#eda100","#4a3aa7","#e87ba4","#73726c","#eb6834"][:len(d["browsers"])])

    page_labels = json.dumps([short_url(p["url"]) for p in d["top_pages"]])
    page_data = json.dumps([p["sessions"] for p in d["top_pages"]])

    event_labels = json.dumps([e["name"] for e in d["smart_events"]])
    event_data = json.dumps([e["sessions"] for e in d["smart_events"]])

    ref_labels = json.dumps([r["name"] for r in d["referrers"]])
    ref_data = json.dumps([r["sessions"] for r in d["referrers"]])
    ref_colors_list = ["#2a78d6","#1baf7a","#eda100","#4a3aa7","#e87ba4","#73726c","#eb6834","#e34948"][:len(d["referrers"])]
    ref_colors = json.dumps(ref_colors_list)

    friction_labels = json.dumps(list(d["insights"].keys()))
    friction_data = json.dumps([v["sessions"] for v in d["insights"].values()])
    friction_colors = json.dumps(["#d85a30" if v["sessions"]>5 else "#eda100" if v["sessions"]>0 else "#1baf7a" for v in d["insights"].values()])

    err_labels = json.dumps([e["name"][:30] for e in d["js_errors"]["errors"]])
    err_data = json.dumps([e["sessions"] for e in d["js_errors"]["errors"]])

    bot_data_vals = [
        d["bot_traffic"].get("suspiciousInteractionBotSessions", 0),
        d["bot_traffic"].get("suspiciousDeviceBotSessions", 0),
        d["bot_traffic"].get("suspiciousNetworkBotSessions", 0),
        d["bot_traffic"].get("webScraperBotSessions", 0),
        d["bot_traffic"].get("ppcAdFraudBotSessions", 0),
        d["bot_traffic"].get("otherBotsSessions", d["bot_traffic"].get("othersSessions", 0)),
    ]
    bot_data = json.dumps(bot_data_vals)
    bot_labels = json.dumps(["Suspicious interaction","Suspicious device","Suspicious network","Web scraper","PPC ad fraud","Other"])

    # Funnel table
    funnel_order = ["Product viewed","Add to cart","Cart viewed","Begin checkout","Checkout","Request quote","Submit form","Contact us","Outbound click"]
    funnel_rows = ""
    prev_count = None
    for ename in funnel_order:
        ev = next((e for e in d["smart_events"] if e["name"] == ename), None)
        if not ev: continue
        drop = ""
        if prev_count and prev_count > 0 and ename not in ("Request quote","Submit form","Contact us","Outbound click"):
            drop = f"-{round((1-ev['sessions']/prev_count)*100)}%"
        hl = ' class="highlight"' if ename.lower() == "request quote" else ""
        funnel_rows += f'<tr{hl}><td>{ename}</td><td>{ev["sessions"]}</td><td>{round(ev["sessions"]/total*100,1)}%</td><td>{drop or "—"}</td></tr>\n'
        if ename not in ("Request quote","Submit form","Contact us","Outbound click"):
            prev_count = ev["sessions"]

    # Bot table
    bot_types = [("Suspicious interaction","suspiciousInteractionBotSessions","Monitor"),
                 ("Suspicious device","suspiciousDeviceBotSessions","Monitor"),
                 ("Suspicious network","suspiciousNetworkBotSessions","Low"),
                 ("Web scraper","webScraperBotSessions","Low"),
                 ("Other","otherBotsSessions","Low"),
                 ("PPC ad fraud","ppcAdFraudBotSessions","")]
    bot_rows = ""
    for label, key, concern in bot_types:
        val = d["bot_traffic"].get(key, 0)
        if label == "PPC ad fraud":
            if val == 0:
                bot_rows += f'<tr style="background:#eaf3de;"><td><strong>{label}</strong></td><td><strong>{val}</strong></td><td style="color:#1d9e75;"><strong>Clear</strong></td></tr>\n'
            else:
                bot_rows += f'<tr style="background:#fcebeb;"><td><strong>{label}</strong></td><td><strong>{val}</strong></td><td style="color:#e34948;"><strong>Alert!</strong></td></tr>\n'
        else:
            cc = "#d85a30" if concern == "Monitor" else "#898781"
            bot_rows += f'<tr><td>{label}</td><td>{val}</td><td style="color:{cc};">{concern}</td></tr>\n'

    # Priority HTML
    p_colors = {"p1":"#e34948","p2":"#d85a30","p3":"#eda100","p4":"#2a78d6","p5":"#898781"}
    prio_html = ""
    for i, (lvl, title, desc) in enumerate(priorities):
        prio_html += f'<div class="priority-box"><div class="priority-num" style="background:{p_colors.get(lvl,"#898781")};">{i+1}</div><div class="priority-content"><h4>{title}</h4><p>{desc}</p></div></div>\n'

    # Referrer legend
    ref_legend = "".join(f'<span><span class="dot" style="background:{ref_colors_list[i]}"></span>{r["name"]} ({r["sessions"]})</span>' for i, r in enumerate(d["referrers"][:8]))

    # History trend chart data (sessions over time)
    has_history = len(history) > 1
    history_labels = json.dumps([h.get("date_start", "?") for h in history]) if has_history else "[]"
    history_sessions = json.dumps([h.get("total_sessions", 0) for h in history]) if has_history else "[]"
    history_lcp = json.dumps([round(h.get("lcp_seconds", 0), 2) for h in history]) if has_history else "[]"
    history_score = json.dumps([round(h.get("perf_score", 0), 1) for h in history]) if has_history else "[]"

    today = datetime.now().strftime("%B %d, %Y")

    # History section HTML
    history_section = ""
    if has_history:
        history_section = f"""
<div class="section">
  <div class="section-title"><span class="num">8</span> Trends over time ({len(history)} data points)</div>
  <div class="section-desc">How key metrics have changed across your imports</div>
  <div class="two-col">
    <div class="chart-box">
      <div class="chart-title">Sessions over time</div>
      <div class="chart-wrap" style="height: 200px;"><canvas id="histSessionsChart"></canvas></div>
    </div>
    <div class="chart-box">
      <div class="chart-title">Performance score over time</div>
      <div class="chart-wrap" style="height: 200px;"><canvas id="histPerfChart"></canvas></div>
    </div>
  </div>
  <div class="chart-box">
    <div class="chart-title">LCP (page load speed) over time</div>
    <div class="chart-wrap" style="height: 200px;"><canvas id="histLcpChart"></canvas></div>
  </div>
  <div class="conclusion">
    <strong>You have {len(history)} data points accumulated.</strong>
    The more weeks you run this script, the more reliable these trends become.
    After 4+ imports you'll start seeing clear patterns in traffic, performance, and engagement.
  </div>
</div>"""

    history_charts_js = ""
    if has_history:
        history_charts_js = f"""
new Chart(document.getElementById('histSessionsChart'), {{
  type: 'line',
  data: {{ labels: {history_labels}, datasets: [{{ label: 'Sessions', data: {history_sessions}, borderColor: '#2a78d6', backgroundColor: 'rgba(42,120,214,0.1)', fill: true, tension: 0.3, pointRadius: 4 }}] }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true, grid: {{ color: '#e5e4df' }} }}, x: {{ grid: {{ display: false }} }} }} }}
}});
new Chart(document.getElementById('histPerfChart'), {{
  type: 'line',
  data: {{ labels: {history_labels}, datasets: [{{ label: 'Score', data: {history_score}, borderColor: '#1baf7a', backgroundColor: 'rgba(27,175,122,0.1)', fill: true, tension: 0.3, pointRadius: 4 }}] }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ min: 50, max: 100, grid: {{ color: '#e5e4df' }} }}, x: {{ grid: {{ display: false }} }} }} }}
}});
new Chart(document.getElementById('histLcpChart'), {{
  type: 'line',
  data: {{ labels: {history_labels}, datasets: [{{ label: 'LCP (s)', data: {history_lcp}, borderColor: '#d85a30', backgroundColor: 'rgba(216,90,48,0.1)', fill: true, tension: 0.3, pointRadius: 4 }}] }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ grid: {{ color: '#e5e4df' }} }}, x: {{ grid: {{ display: false }} }} }} }}
}});"""

    # Data history badge
    history_badge = ""
    if import_count > 1:
        history_badge = f' · {import_count} imports in database'

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MasonMart Clarity Report — {d["date_range"]}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',-apple-system,sans-serif;background:#f8f7f4;color:#1a1a1a;line-height:1.7}}
.container{{max-width:900px;margin:0 auto;padding:2rem 1.5rem}}
.header{{text-align:center;padding:2.5rem 0 2rem;border-bottom:2px solid #1a1a1a;margin-bottom:2rem}}
.header h1{{font-size:28px;font-weight:600;letter-spacing:-0.5px}}
.header .subtitle{{color:#6b6a66;font-size:14px;margin-top:6px}}
.header .date-badge{{display:inline-block;background:#2a78d6;color:#fff;font-size:12px;padding:4px 14px;border-radius:20px;margin-top:10px}}
.kpi-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:2.5rem}}
.kpi{{background:#fff;border:1px solid #e5e4df;border-radius:10px;padding:1rem 1.1rem}}
.kpi .label{{font-size:12px;color:#898781;text-transform:uppercase;letter-spacing:0.5px}}
.kpi .value{{font-size:26px;font-weight:600;margin-top:2px}}
.kpi .delta{{font-size:11px;margin-top:2px}}
.section{{margin-bottom:2.5rem}}
.section-title{{font-size:18px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:8px}}
.section-title .num{{background:#1a1a1a;color:#fff;font-size:12px;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center}}
.section-desc{{font-size:14px;color:#6b6a66;margin-bottom:1rem}}
.chart-box{{background:#fff;border:1px solid #e5e4df;border-radius:10px;padding:1.25rem;margin-bottom:1rem}}
.chart-title{{font-size:14px;font-weight:600;margin-bottom:12px}}
.chart-wrap{{position:relative;width:100%}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:#6b6a66;margin-bottom:10px}}
.legend span{{display:flex;align-items:center;gap:5px}}
.legend .dot{{width:10px;height:10px;border-radius:2px;display:inline-block}}
.conclusion{{background:#fff;border-left:3px solid #2a78d6;border-radius:0 8px 8px 0;padding:1rem 1.25rem;margin-top:10px;margin-bottom:6px;font-size:14px}}
.conclusion strong{{color:#2a78d6}}
.conclusion.warn{{border-left-color:#d85a30}}.conclusion.warn strong{{color:#d85a30}}
.conclusion.good{{border-left-color:#1d9e75}}.conclusion.good strong{{color:#1d9e75}}
.conclusion.critical{{border-left-color:#e34948}}.conclusion.critical strong{{color:#e34948}}
.data-table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
.data-table th{{text-align:left;font-weight:600;padding:8px 10px;border-bottom:2px solid #1a1a1a;font-size:12px;text-transform:uppercase;letter-spacing:0.4px;color:#52514e}}
.data-table td{{padding:8px 10px;border-bottom:1px solid #e5e4df}}
.data-table tr:last-child td{{border-bottom:none}}
.data-table .highlight{{background:#fef3e8;font-weight:600}}
.priority-box{{background:#fff;border:1px solid #e5e4df;border-radius:10px;padding:1.25rem;margin-bottom:10px;display:flex;gap:14px;align-items:flex-start}}
.priority-num{{color:#fff;font-size:14px;font-weight:700;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0}}
.priority-content h4{{font-size:14px;font-weight:600;margin-bottom:3px}}
.priority-content p{{font-size:13px;color:#52514e}}
.score-meter{{height:10px;background:#e5e4df;border-radius:5px;overflow:hidden;margin:6px 0}}
.score-fill{{height:100%;border-radius:5px}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.footer{{text-align:center;padding:2rem 0 1rem;border-top:1px solid #e5e4df;margin-top:2rem;color:#898781;font-size:12px}}
@media(max-width:600px){{.kpi-row{{grid-template-columns:repeat(2,1fr)}}.two-col{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="container">
<div class="header">
  <h1>MasonMart website behavior report</h1>
  <div class="subtitle">Microsoft Clarity data analysis — masonmart.in</div>
  <div class="date-badge">{d["date_range"]} ({total} sessions){history_badge}</div>
</div>

<div class="kpi-row">
  <div class="kpi"><div class="label">Real sessions</div><div class="value">{total}</div><div class="delta">{sessions_trend}</div></div>
  <div class="kpi"><div class="label">Pages / session</div><div class="value">{d["pages_per_session"]}</div><div class="delta">{pps_trend}</div></div>
  <div class="kpi"><div class="label">Avg scroll depth</div><div class="value">{d["scroll_depth"]}%</div><div class="delta">{scroll_trend}</div></div>
  <div class="kpi"><div class="label">Quick-back rate</div><div class="value" style="color:{"#d85a30" if qb_pct>20 else "#1a1a1a"};">{qb_pct}%</div><div class="delta">{qb.get("sessions",0)} of {total} sessions</div></div>
</div>

<div class="section">
  <div class="section-title"><span class="num">1</span> Device and browser split</div>
  <div class="two-col">
    <div class="chart-box"><div class="chart-title">Mobile vs desktop</div>
      <div class="legend"><span><span class="dot" style="background:#2a78d6"></span>Mobile {mobile_pct}%</span><span><span class="dot" style="background:#73726c"></span>Desktop {round(100-mobile_pct,1)}%</span></div>
      <div class="chart-wrap" style="height:200px;"><canvas id="deviceChart"></canvas></div></div>
    <div class="chart-box"><div class="chart-title">Browser breakdown</div>
      <div class="chart-wrap" style="height:200px;"><canvas id="browserChart"></canvas></div></div>
  </div>
  {sec_conclusions("device")}
</div>

<div class="section">
  <div class="section-title"><span class="num">2</span> Traffic sources</div>
  <div class="chart-box"><div class="chart-title">Session sources</div>
    <div class="legend">{ref_legend}</div>
    <div class="chart-wrap" style="height:220px;"><canvas id="sourceChart"></canvas></div></div>
  {sec_conclusions("users")}
</div>

<div class="section">
  <div class="section-title"><span class="num">3</span> Most visited pages</div>
  <div class="chart-box"><div class="chart-title">Sessions by page</div>
    <div class="chart-wrap" style="height:{max(280,len(d["top_pages"])*28+40)}px;"><canvas id="pagesChart"></canvas></div></div>
  {sec_conclusions("pages")}
</div>

<div class="section">
  <div class="section-title"><span class="num">4</span> Shopping funnel</div>
  <div class="chart-box"><div class="chart-title">Smart events by session count</div>
    <div class="chart-wrap" style="height:{max(250,len(d["smart_events"])*26+40)}px;"><canvas id="funnelChart"></canvas></div></div>
  <table class="data-table"><thead><tr><th>Step</th><th>Sessions</th><th>% of total</th><th>Drop</th></tr></thead><tbody>{funnel_rows}</tbody></table>
  {sec_conclusions("funnel")}
</div>

<div class="section">
  <div class="section-title"><span class="num">5</span> Friction signals</div>
  <div class="chart-box"><div class="chart-title">Friction events</div>
    <div class="chart-wrap" style="height:220px;"><canvas id="frictionChart"></canvas></div></div>
  {sec_conclusions("friction")}
</div>

<div class="section">
  <div class="section-title"><span class="num">6</span> Performance and technical health</div>
  <div class="two-col">
    <div class="chart-box"><div class="chart-title">Performance score</div>
      <div style="font-size:42px;font-weight:700;text-align:center;padding:10px 0;">{round(perf_score)}<span style="font-size:18px;color:#898781;">/100</span></div>
      <div class="score-meter"><div class="score-fill" style="width:{min(100,perf_score)}%;background:{score_color};"></div></div>
      <div style="font-size:12px;color:#898781;text-align:center;">{perf_trend}</div></div>
    <div class="chart-box"><div class="chart-title">Core Web Vitals</div>
      <table class="data-table" style="margin-top:0;">
        <tr><td>LCP</td><td style="font-weight:600;color:{lcp_color};">{d["performance"]["lcp"]}</td><td style="font-size:11px;">{lcp_trend}</td></tr>
        <tr><td>INP</td><td style="font-weight:600;color:{inp_color};">{d["performance"]["inp"]}</td><td style="font-size:11px;">{inp_trend}</td></tr>
        <tr><td>CLS</td><td style="font-weight:600;color:{cls_color};">{d["performance"]["cls"]}</td><td style="font-size:11px;color:#898781;">Target: &lt;0.1</td></tr>
      </table></div>
  </div>
  {"" if not d["js_errors"]["errors"] else f'<div class="chart-box"><div class="chart-title">JS errors ({d["js_errors"]["total"]} sessions)</div><div class="chart-wrap" style="height:200px;"><canvas id="jsErrorChart"></canvas></div></div>'}
  {sec_conclusions("perf")}
</div>

<div class="section">
  <div class="section-title"><span class="num">7</span> Bot traffic</div>
  <div class="chart-box"><div class="chart-title">Bot type breakdown</div>
    <div class="chart-wrap" style="height:220px;"><canvas id="botChart"></canvas></div></div>
  <table class="data-table"><thead><tr><th>Bot type</th><th>Sessions</th><th>Concern</th></tr></thead><tbody>{bot_rows}</tbody></table>
  {sec_conclusions("bot")}
</div>

{history_section}

<div class="section">
  <div class="section-title" style="font-size:20px;">Priority actions</div>
  <div class="section-desc">Ranked by potential revenue impact</div>
  {prio_html}
</div>

<div class="footer">
  MasonMart Clarity Report — Generated {today} · Data: {d["date_range"]} · {total} sessions<br>
  Generated by clarity_report.py · No API keys or cloud services required · Data stored in {DB_NAME}
</div>
</div>

<script>
new Chart(document.getElementById('deviceChart'),{{type:'doughnut',data:{{labels:['Mobile','Desktop'],datasets:[{{data:[{mobile_sessions},{desktop_sessions}],backgroundColor:['#2a78d6','#73726c'],borderWidth:2,borderColor:'#fff'}}]}},options:{{responsive:true,maintainAspectRatio:false,cutout:'55%',plugins:{{legend:{{display:false}}}}}}}});
new Chart(document.getElementById('browserChart'),{{type:'bar',data:{{labels:{browser_labels},datasets:[{{data:{browser_data},backgroundColor:{browser_colors},borderRadius:4,maxBarThickness:18}}]}},options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{beginAtZero:true,grid:{{color:'#e5e4df'}}}},y:{{grid:{{display:false}},ticks:{{font:{{size:10}}}}}}}}}}}});
new Chart(document.getElementById('sourceChart'),{{type:'doughnut',data:{{labels:{ref_labels},datasets:[{{data:{ref_data},backgroundColor:{ref_colors},borderWidth:2,borderColor:'#fff'}}]}},options:{{responsive:true,maintainAspectRatio:false,cutout:'55%',plugins:{{legend:{{display:false}}}}}}}});
new Chart(document.getElementById('pagesChart'),{{type:'bar',data:{{labels:{page_labels},datasets:[{{data:{page_data},backgroundColor:{page_data}.map((v,i)=>i===0?'#2a78d6':'#b5d4f4'),borderRadius:4,maxBarThickness:18}}]}},options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{beginAtZero:true,grid:{{color:'#e5e4df'}}}},y:{{grid:{{display:false}},ticks:{{font:{{size:11}}}}}}}}}}}});
new Chart(document.getElementById('funnelChart'),{{type:'bar',data:{{labels:{event_labels},datasets:[{{data:{event_data},backgroundColor:{event_data}.map((v,i)=>{{var n={event_labels}[i];return n==='Request quote'?'#e34948':'#2a78d6';}}),borderRadius:4,maxBarThickness:18}}]}},options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{beginAtZero:true,grid:{{color:'#e5e4df'}}}},y:{{grid:{{display:false}},ticks:{{font:{{size:11}}}}}}}}}}}});
new Chart(document.getElementById('frictionChart'),{{type:'bar',data:{{labels:{friction_labels},datasets:[{{data:{friction_data},backgroundColor:{friction_colors},borderRadius:4,maxBarThickness:24}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{y:{{beginAtZero:true,grid:{{color:'#e5e4df'}},ticks:{{stepSize:5}}}},x:{{grid:{{display:false}}}}}}}}}});
var jsC=document.getElementById('jsErrorChart');
if(jsC){{new Chart(jsC,{{type:'bar',data:{{labels:{err_labels},datasets:[{{data:{err_data},backgroundColor:['#e34948','#d85a30','#d85a30','#eda100','#eda100','#898781'].slice(0,{len(d["js_errors"]["errors"])}),borderRadius:4,maxBarThickness:24}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{y:{{beginAtZero:true,grid:{{color:'#e5e4df'}},ticks:{{stepSize:1}}}},x:{{grid:{{display:false}}}}}}}}}});}}
new Chart(document.getElementById('botChart'),{{type:'bar',data:{{labels:{bot_labels},datasets:[{{data:{bot_data},backgroundColor:{bot_data}.map((v,i)=>i===4?'#1baf7a':'#73726c'),borderRadius:4,maxBarThickness:22}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{y:{{beginAtZero:true,grid:{{color:'#e5e4df'}}}},x:{{grid:{{display:false}},ticks:{{font:{{size:10}},maxRotation:45}}}}}}}}}});
{history_charts_js}
</script>
</body>
</html>'''
    return html


# ─────────────────────────────────────────────
# 6. MAIN
# ─────────────────────────────────────────────

def load_latest_from_db(conn):
    """Load the most recent CSV import's data from the database, without needing a CSV file."""
    row = conn.execute(
        "SELECT * FROM imports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None, None

    cols = [d[0] for d in conn.execute("SELECT * FROM imports LIMIT 0").description]
    import_row = dict(zip(cols, row))
    import_id = import_row["id"]

    # Load the snapshot
    snap = conn.execute("SELECT * FROM snapshots WHERE import_id = ?", (import_id,)).fetchone()
    if not snap:
        return None, None
    snap_cols = [d[0] for d in conn.execute("SELECT * FROM snapshots LIMIT 0").description]
    snap_dict = dict(zip(snap_cols, snap))

    # Rebuild the full data dict from stored tables
    insights = {}
    for r in conn.execute("SELECT name, sessions, pct FROM insights WHERE import_id=?", (import_id,)):
        insights[r[0]] = {"sessions": r[1], "pct": r[2]}

    browsers = [{"name": r[0], "sessions": r[1], "pct": r[2]}
                for r in conn.execute("SELECT name, sessions, pct FROM browsers WHERE import_id=?", (import_id,))]

    top_pages = [{"url": r[0], "sessions": r[1]}
                 for r in conn.execute("SELECT url, sessions FROM top_pages WHERE import_id=?", (import_id,))]

    smart_events = [{"name": r[0], "sessions": r[1], "pct": r[2]}
                    for r in conn.execute("SELECT name, sessions, pct FROM smart_events WHERE import_id=?", (import_id,))]

    referrers = [{"name": r[0], "sessions": r[1]}
                 for r in conn.execute("SELECT name, sessions FROM referrers WHERE import_id=?", (import_id,))]

    js_errs = [{"name": r[0], "sessions": r[1], "pct": r[2]}
               for r in conn.execute("SELECT name, sessions, pct FROM js_errors WHERE import_id=?", (import_id,))]

    bot_traffic = {}
    for r in conn.execute("SELECT bot_type, sessions FROM bot_traffic WHERE import_id=?", (import_id,)):
        bot_traffic[r[0]] = r[1]

    data = {
        "project_name": "", "date_range": import_row.get("date_range", ""),
        "sessions": {"total": snap_dict.get("total_sessions", 0), "bot": snap_dict.get("bot_sessions", 0)},
        "pages_per_session": snap_dict.get("pages_per_session", 0),
        "scroll_depth": snap_dict.get("scroll_depth", 0),
        "active_time": snap_dict.get("active_time", 0),
        "total_time": snap_dict.get("total_time", 0),
        "users": {"unique": snap_dict.get("unique_users", 0),
                  "new": snap_dict.get("new_user_sessions", 0),
                  "returning": snap_dict.get("returning_user_sessions", 0)},
        "insights": insights, "browsers": browsers, "top_pages": top_pages,
        "smart_events": smart_events, "referrers": referrers,
        "js_errors": {"total": snap_dict.get("js_error_sessions", 0), "errors": js_errs},
        "performance": {"score": snap_dict.get("perf_score", 0),
                        "lcp": f"{snap_dict.get('lcp_seconds', 0)}s",
                        "inp": f"{int(snap_dict.get('inp_ms', 0))}ms",
                        "cls": f"{snap_dict.get('cls_value', 0)}s"},
        "bot_traffic": bot_traffic,
    }
    return data, import_id


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python clarity_report.py <path_to_csv>   Import CSV and generate report")
        print("  python clarity_report.py --latest         Re-generate report from last imported CSV")
        print("\nEach CSV you feed gets stored in a local database (clarity_history.sqlite).")
        print("Reports compare current data against previous imports automatically.")
        sys.exit(1)

    use_latest = sys.argv[1] == "--latest"

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    init_db(conn)
    print(f"Database: {db_path}")

    if use_latest:
        # Generate report from the most recent CSV already in the database
        data, import_id = load_latest_from_db(conn)
        if not data:
            print("No CSV imports found in database. Import one first:")
            print("  python clarity_report.py path/to/Clarity_export.csv")
            sys.exit(1)
        print(f"Using latest import: {data['date_range']}")
        print(f"  Sessions: {data['sessions']['total']} real, {data['sessions']['bot']} bot")
    else:
        csv_path = sys.argv[1]
        if not os.path.exists(csv_path):
            print(f"File not found: {csv_path}")
            sys.exit(1)
        print(f"Parsing: {csv_path}")
        data = parse_clarity_csv(csv_path)
        print(f"  Project: {data['project_name']}")
        print(f"  Date range: {data['date_range']}")
        print(f"  Sessions: {data['sessions']['total']} real, {data['sessions']['bot']} bot")
        csv_filename = os.path.basename(csv_path)
        import_id = store_data(conn, data, csv_filename)

    import_count = count_imports(conn)
    print(f"  Total imports in database: {import_count}")

    prev = load_previous_snapshot(conn, import_id)
    if prev:
        print(f"  Previous snapshot found (import #{prev['import_id']}) — trend comparison enabled")
    else:
        print(f"  No previous data — first import, no trends yet")

    history = load_history(conn)
    print(f"  History points available: {len(history)}")

    conclusions = generate_conclusions(data, prev)
    print(f"  {len(conclusions)} conclusions generated")

    priorities = generate_priorities(data, conclusions)
    print(f"  {len(priorities)} action items")

    html = build_html(data, conclusions, priorities, prev, history, import_count)

    today = datetime.now().strftime("%Y-%m-%d")
    out_path = os.path.join(os.getcwd(), f"MasonMart_Clarity_Report_{today}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nReport saved: {out_path}")
    print("Open it in any browser to view.")

    conn.close()


if __name__ == "__main__":
    main()