"""
weekly_report.py
----------------
Reads the ENTIRE history accumulated by fetch_daily.py in
clarity_history.sqlite and produces a weekly summary report.

It does NOT hit the Clarity API. It just re-uses the data the daily
job has already collected, so it costs nothing and needs no token.

What it does
------------
  * Pulls a few important KPIs (sessions, unique users, pages/session,
    bot share) for THIS week and compares them to LAST week and to the
    all-time baseline.
  * Highlights STRENGTHENED areas (things getting better) and
    WEAKENED areas (things getting worse).
  * Surfaces the top page and traffic-source MOVERS week-over-week
    from the stored dimension breakdowns.

Output
------
  MasonMart_Weekly_Report_<YYYY-MM-DD>.html   (full report)
  weekly_summary_<YYYY-MM-DD>.csv             (the KPI table)

USAGE
-----
  python weekly_report.py

REQUIREMENTS
------------
  Python 3.8+  (standard library only)
"""

import os
import csv
import json
import sqlite3
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "clarity_history.sqlite")

WINDOW_DAYS = 7  # size of "this week" / "last week"


# ─────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────

def load_daily_totals(conn):
    """Return list of dict rows from daily_totals, oldest first."""
    try:
        cur = conn.execute(
            "SELECT fetch_date, total_sessions, bot_sessions, unique_users, "
            "pages_per_session FROM daily_totals ORDER BY fetch_date ASC")
    except sqlite3.OperationalError:
        return []
    rows = []
    for r in cur.fetchall():
        rows.append({
            "date": r[0],
            "sessions": r[1] or 0,
            "bots": r[2] or 0,
            "users": r[3] or 0,
            "pps": r[4] or 0.0,
        })
    return rows


def window_bounds(rows):
    """Split available dates into (this_week_dates, last_week_dates).

    Windows are anchored to the most recent date present in the data, so
    a gap of a missing day or two never breaks the comparison.
    """
    if not rows:
        return [], []
    dates = [r["date"] for r in rows]
    latest = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    this_start = latest - timedelta(days=WINDOW_DAYS - 1)
    last_end = this_start - timedelta(days=1)
    last_start = last_end - timedelta(days=WINDOW_DAYS - 1)

    def in_range(d, start, end):
        dd = datetime.strptime(d, "%Y-%m-%d").date()
        return start <= dd <= end

    this_week = [r for r in rows if in_range(r["date"], this_start, latest)]
    last_week = [r for r in rows if in_range(r["date"], last_start, last_end)]
    return this_week, last_week


def aggregate(window):
    """Roll a list of daily rows into weekly KPIs."""
    if not window:
        return None
    sessions = sum(r["sessions"] for r in window)
    bots = sum(r["bots"] for r in window)
    users = sum(r["users"] for r in window)
    # pages/session: weight each day's value by that day's sessions
    weight = sum(r["sessions"] for r in window)
    if weight > 0:
        pps = sum(r["sessions"] * r["pps"] for r in window) / weight
    else:
        pps = sum(r["pps"] for r in window) / len(window)
    total_traffic = sessions + bots
    bot_share = (bots / total_traffic * 100) if total_traffic > 0 else 0.0
    return {
        "days": len(window),
        "sessions": sessions,
        "bots": bots,
        "users": users,
        "pps": round(pps, 2),
        "bot_share": round(bot_share, 1),
    }


# ─────────────────────────────────────────────
# Dimension movers (top pages / sources)
# ─────────────────────────────────────────────

def load_dimension(conn, dim_name, dates):
    """Aggregate sessions per value of a dimension over a set of dates.

    Parses the raw_json stored by fetch_daily.py. Returns {value: sessions}.
    Degrades to {} if the data isn't shaped as expected.
    """
    if not dates:
        return {}
    totals = {}
    try:
        placeholders = ",".join("?" for _ in dates)
        cur = conn.execute(
            "SELECT dimensions, raw_json FROM api_fetches "
            "WHERE fetch_date IN (%s)" % placeholders, list(dates))
    except sqlite3.OperationalError:
        return {}

    for dims, raw in cur.fetchall():
        if not dims or dim_name.lower() not in dims.lower():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            # Only "Traffic" carries real session counts per dimension value;
            # the other metric blocks (DeadClickCount, ScrollDepth, ...) use
            # a different, unrelated field shape and would corrupt the total.
            if block.get("metricName") != "Traffic":
                continue
            for item in block.get("information", []):
                if not isinstance(item, dict):
                    continue
                value = item.get(dim_name)
                if value in (None, ""):
                    # try case-insensitive key match
                    for k in item:
                        if k.lower() == dim_name.lower():
                            value = item[k]
                            break
                if value in (None, ""):
                    continue
                if dim_name.lower() == "url":
                    # collapse query-string variants of the same page
                    value = value.split("?", 1)[0]
                try:
                    s = int(item.get("totalSessionCount", 0) or 0)
                except (ValueError, TypeError):
                    s = 0
                totals[value] = totals.get(value, 0) + s
    return totals


def top_movers(conn, dim_name, this_dates, last_dates, limit=8):
    """Return list of dicts for the top values this week with WoW delta.

    If there's no last-week data at all, prev/delta are left as None rather
    than 0, so the report never implies a comparison that doesn't exist.
    """
    this = load_dimension(conn, dim_name, this_dates)
    last = load_dimension(conn, dim_name, last_dates) if last_dates else None
    if not this:
        return []
    ranked = sorted(this.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    out = []
    for value, cur in ranked:
        if last is None:
            prev, delta = None, None
        else:
            prev = last.get(value, 0)
            delta = cur - prev
        out.append({
            "value": value,
            "sessions": cur,
            "prev": prev,
            "delta": delta,
        })
    return out


# ─────────────────────────────────────────────
# Strengthened / weakened classification
# ─────────────────────────────────────────────

def pct_change(now, then):
    if then in (0, None):
        return None if not now else 100.0
    return round((now - then) / then * 100, 1)


def classify(this, last):
    """Return (strengthened, weakened) lists of human-readable strings.

    'higher_better' flags encode which direction is good per metric.
    """
    if not this or not last:
        return [], []

    metrics = [
        ("Sessions", this["sessions"], last["sessions"], True, "{:,}"),
        ("Unique users", this["users"], last["users"], True, "{:,}"),
        ("Pages / session", this["pps"], last["pps"], True, "{:.2f}"),
        ("Bot share", this["bot_share"], last["bot_share"], False, "{:.1f}%"),
    ]
    strengthened, weakened = [], []
    for name, now, then, higher_better, fmt in metrics:
        chg = pct_change(now, then)
        if chg is None or chg == 0:
            continue
        improved = (chg > 0) if higher_better else (chg < 0)
        arrow = "▲" if chg > 0 else "▼"
        line = "%s %s %s (%s → %s, %+.1f%%)" % (
            name, arrow, "up" if chg > 0 else "down",
            fmt.format(then), fmt.format(now), chg)
        (strengthened if improved else weakened).append(line)
    return strengthened, weakened


# ─────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def kpi_row(label, this, last, fmt="{:,}"):
    tv = fmt.format(this) if this is not None else "—"
    lv = fmt.format(last) if last is not None else "—"
    chg = pct_change(this, last) if (this is not None and last is not None) else None
    if chg is None:
        cell = '<td class="muted">—</td>'
    else:
        cls = "up" if chg > 0 else ("down" if chg < 0 else "muted")
        cell = '<td class="%s">%+.1f%%</td>' % (cls, chg)
    return "<tr><td>%s</td><td>%s</td><td>%s</td>%s</tr>" % (
        esc(label), lv, tv, cell)


def solo_kpi_table(this):
    """Absolute-numbers table for when there's no prior week to compare to."""
    return (
        "<table><thead><tr><th>Metric</th><th>This week</th></tr></thead><tbody>"
        "<tr><td>Sessions</td><td>{:,}</td></tr>"
        "<tr><td>Unique users</td><td>{:,}</td></tr>"
        "<tr><td>Pages / session</td><td>{:.2f}</td></tr>"
        "<tr><td>Bot sessions</td><td>{:,}</td></tr>"
        "<tr><td>Bot share</td><td>{:.1f}%</td></tr>"
        "</tbody></table>".format(
            this["sessions"], this["users"], this["pps"],
            this["bots"], this["bot_share"]))


def daily_trend_table(rows):
    """Plain day-by-day sessions/users table — always real, always available."""
    if not rows:
        return ""
    body = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(r["date"]), "{:,}".format(r["sessions"]),
            "{:,}".format(r["users"]), "{:,}".format(r["bots"]))
        for r in rows)
    return (
        "<h2>Day by day (most recent %d days)</h2>"
        "<table><thead><tr><th>Date</th><th>Sessions</th>"
        "<th>Unique users</th><th>Bot sessions</th></tr></thead>"
        "<tbody>%s</tbody></table>" % (len(rows), body))


def movers_table(title, movers):
    if not movers:
        return ""
    has_comparison = movers[0]["prev"] is not None
    rows = []
    for m in movers:
        if not has_comparison:
            rows.append("<tr><td>%s</td><td>%s</td></tr>" % (
                esc(m["value"]), "{:,}".format(m["sessions"])))
            continue
        if m["delta"] > 0:
            cls, arrow = "up", "▲"
        elif m["delta"] < 0:
            cls, arrow = "down", "▼"
        else:
            cls, arrow = "muted", "—"
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td>"
            '<td class="%s">%s %+d</td></tr>' % (
                esc(m["value"]), "{:,}".format(m["sessions"]),
                "{:,}".format(m["prev"]), cls, arrow, m["delta"]))
    if has_comparison:
        head = "<th>%s</th><th>This week</th><th>Last week</th><th>Change</th>" % esc(title)
    else:
        head = "<th>%s</th><th>This week</th>" % esc(title)
    return (
        "<h2>%s</h2>"
        "<table><thead><tr>%s</tr></thead>"
        "<tbody>%s</tbody></table>" % (esc(title), head, "".join(rows)))


def build_html(this, last, baseline, strengthened, weakened,
               page_movers, source_movers, generated, recent_rows):
    def li(items, cls):
        if not items:
            return '<li class="muted">None</li>'
        return "".join('<li class="%s">%s</li>' % (cls, esc(x)) for x in items)

    kpis = ""
    if this and last:
        kpis = (
            "<table><thead><tr><th>Metric</th><th>Last week</th>"
            "<th>This week</th><th>WoW</th></tr></thead><tbody>"
            + kpi_row("Sessions", this["sessions"], last["sessions"])
            + kpi_row("Unique users", this["users"], last["users"])
            + kpi_row("Pages / session", this["pps"], last["pps"], "{:.2f}")
            + kpi_row("Bot sessions", this["bots"], last["bots"])
            + kpi_row("Bot share", this["bot_share"], last["bot_share"], "{:.1f}%")
            + "</tbody></table>")
    elif this:
        kpis = (
            "<p class='muted'>Not enough history yet for a week-over-week "
            "comparison — showing this week's real totals instead.</p>"
            + solo_kpi_table(this))
    else:
        kpis = "<p class='muted'>No data available yet.</p>"

    base_note = ""
    if baseline and this:
        base_note = (
            "<p class='muted'>All-time baseline (per-week avg over %d days): "
            "%s sessions/wk · %s users/wk.</p>" % (
                baseline["total_days"],
                "{:,}".format(baseline["avg_sessions_week"]),
                "{:,}".format(baseline["avg_users_week"])))

    if this and last:
        changed_section = (
            "<h2>What changed</h2><div class='cols'>"
            "<div class='card good'><h3>Strengthened</h3><ul>%s</ul></div>"
            "<div class='card bad'><h3>Weakened</h3><ul>%s</ul></div></div>"
            % (li(strengthened, "up"), li(weakened, "down")))
    else:
        changed_section = ""

    trend = daily_trend_table(recent_rows)

    return """<!doctype html>
<html><head><meta charset="utf-8">
<title>Mason Mart — Weekly Clarity Report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 900px; margin: 30px auto; padding: 0 16px; color: #1a1a1a; }}
  h1 {{ margin-bottom: 4px; }}
  .sub {{ color: #666; margin-top: 0; }}
  h2 {{ margin-top: 34px; border-bottom: 2px solid #eee; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }}
  th {{ background: #fafafa; }}
  td:nth-child(n+2), th:nth-child(n+2) {{ text-align: right; }}
  .up {{ color: #12805c; font-weight: 600; }}
  .down {{ color: #c0392b; font-weight: 600; }}
  .muted {{ color: #999; }}
  .cols {{ display: flex; gap: 24px; flex-wrap: wrap; }}
  .card {{ flex: 1; min-width: 260px; background: #fafafa;
           border: 1px solid #eee; border-radius: 10px; padding: 14px 18px; }}
  .card.good h3 {{ color: #12805c; }}
  .card.bad h3 {{ color: #c0392b; }}
  ul {{ margin: 8px 0; padding-left: 18px; }}
  li {{ margin: 4px 0; }}
  footer {{ margin-top: 40px; color: #aaa; font-size: 12px; }}
</style></head><body>
<h1>Mason Mart — Weekly Clarity Report</h1>
<p class="sub">Generated {generated}</p>

<h2>Key metrics — this week vs last week</h2>
{kpis}
{base_note}

{changed_section}

{trend}
{pages}
{sources}

<footer>Built from clarity_history.sqlite — the rolling history collected by
fetch_daily.py. All figures above are the raw stored totals; nothing here is
estimated or invented. No API calls were made to generate this report.</footer>
</body></html>""".format(
        generated=esc(generated),
        kpis=kpis,
        base_note=base_note,
        changed_section=changed_section,
        trend=trend,
        pages=movers_table("Top pages", page_movers),
        sources=movers_table("Top traffic sources", source_movers),
    )


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    if not os.path.exists(DB_PATH):
        print("ERROR: %s not found. Run fetch_daily.py first." % DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    rows = load_daily_totals(conn)
    if not rows:
        print("No daily_totals rows yet — nothing to report.")
        conn.close()
        return 0

    this_rows, last_rows = window_bounds(rows)
    this = aggregate(this_rows)
    last = aggregate(last_rows)

    # all-time baseline (average per 7-day week across full history)
    baseline = None
    total_days = len(rows)
    if total_days:
        weeks = max(total_days / WINDOW_DAYS, 1)
        baseline = {
            "total_days": total_days,
            "avg_sessions_week": round(sum(r["sessions"] for r in rows) / weeks),
            "avg_users_week": round(sum(r["users"] for r in rows) / weeks),
        }

    strengthened, weakened = classify(this, last)

    this_dates = [r["date"] for r in this_rows]
    last_dates = [r["date"] for r in last_rows]
    page_movers = top_movers(conn, "URL", this_dates, last_dates)
    source_movers = top_movers(conn, "Source", this_dates, last_dates)
    conn.close()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    recent_rows = list(reversed(rows[-14:]))  # newest first, at most 2 weeks
    html = build_html(this, last, baseline, strengthened, weakened,
                      page_movers, source_movers, generated, recent_rows)
    html_path = os.path.join(SCRIPT_DIR, "MasonMart_Weekly_Report_%s.html" % stamp)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # CSV of the KPI comparison
    csv_path = os.path.join(SCRIPT_DIR, "weekly_summary_%s.csv" % stamp)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        metrics = ["sessions", "users", "pps", "bots", "bot_share"]
        labels = ["sessions", "unique_users", "pages_per_session",
                  "bot_sessions", "bot_share_pct"]
        if this and last:
            w.writerow(["metric", "last_week", "this_week", "wow_pct"])
            for label, key in zip(labels, metrics):
                w.writerow([label, last[key], this[key],
                            pct_change(this[key], last[key])])
        elif this:
            w.writerow(["metric", "this_week"])
            for label, key in zip(labels, metrics):
                w.writerow([label, this[key]])

    print("Wrote:")
    print("  " + html_path)
    print("  " + csv_path)
    if this and last:
        print("This week: %s sessions, %s users, %.2f pages/session, %.1f%% bots"
              % (this["sessions"], this["users"], this["pps"], this["bot_share"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
