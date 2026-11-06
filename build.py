#!/usr/bin/env python3
"""
בונה דף אינטרנט פשוט ונגיש עם לוח ההקרנות בסינמטק תל אביב ובקולנוע לב.
רץ כל בוקר ב-GitHub Actions ושומר את הדף בתיקייה _site.

הרצה מקומית:   python build.py
בדיקה מקובץ שמור: python build.py --cinematheque-file tests/cinematheque-sample.html
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------- הגדרות ----------------
DAYS_AHEAD = 7              # כמה ימים להציג (היום + עוד 6)
HIDE_FAMILY_SCREENINGS = False  # True = להסתיר הקרנות "לכל המשפחה" (ילדים/מדובב)

CINEMATHEQUE_URL = "https://www.cinema.co.il/shown/?date={date}"
CINEMATHEQUE_HOME = "https://www.cinema.co.il/shown/"

LEV_NAME = "קולנוע לב דיזנגוף"
LEV_URL = "https://www.lev.co.il/"   # יעודכן כשנדע את כתובת לוח ההקרנות המדויקת

TZ = ZoneInfo("Asia/Jerusalem")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal schedule page for a family member)",
    "Accept-Language": "he-IL,he;q=0.9",
}
FAMILY_CAT = "movie-cat-10"
HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
# ----------------------------------------


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


# ======== סינמטק תל אביב ========
def parse_cinematheque(page_html):
    """מחזיר רשימת הקרנות מתוך עמוד 'לוח ההקרנות' (לשונית 'הצג לפי שעה')."""
    soup = BeautifulSoup(page_html, "html.parser")
    pane = soup.find(id="nav-date")
    if pane is None:
        raise ValueError("לא נמצאה לשונית 'הצג לפי שעה' (nav-date) – ייתכן שמבנה האתר השתנה")

    shows = []
    for group in pane.select("div.fest-box-parent-wrapper"):
        title_el = group.find(class_="by-date-title")
        group_time = clean(title_el.get_text()) if title_el else ""
        for box in group.select("div.festival-grid-box"):
            classes = box.get("class", [])
            if HIDE_FAMILY_SCREENINGS and FAMILY_CAT in classes:
                continue
            a = box.select_one(".title h3 a")
            if not a:
                continue
            meta_el = box.select_one(".title p")
            meta = clean(meta_el.get_text()) if meta_el else ""
            meta = re.sub(r"\s*/\s*", " · ", meta).strip(" ·")
            meta = meta.replace("אורך:", "אורך: ").replace("אורך:  ", "אורך: ")
            if re.search(r"אורך: \d+$", meta):
                meta += " דק׳"
            lang = director = ""
            for h4 in box.select(".paragraph h4"):
                t = clean(h4.get_text())
                if t.startswith("שפה:"):
                    lang = t.replace("שפה:", "").strip()
                elif t.startswith("בימוי:"):
                    director = t.replace("בימוי:", "").strip()
            p = box.select_one(".paragraph p")
            short_desc = clean(p.get_text()) if p else ""
            badge = box.select_one(".text-badge .badg")
            time = clean(badge.get_text()) if badge else group_time
            order = box.select_one("a.order-btn")
            shows.append({
                "time": time,
                "title": clean(a.get_text()),
                "meta": meta,
                "lang": lang,
                "director": director,
                "desc": short_desc,
                "family": FAMILY_CAT in classes,
                "link": order["href"] if order and order.has_attr("href") else a["href"],
                "info": a["href"],
            })
    shows.sort(key=lambda s: s["time"])
    return shows


def fetch_cinematheque(date):
    r = requests.get(CINEMATHEQUE_URL.format(date=date.isoformat()), headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_cinematheque(r.text)


def extract_full_description(event_html, short_desc):
    """
    מוצא בעמוד הסרט את התיאור המלא: האלמנט הקטן ביותר שמכיל את תחילת
    התיאור המקוצר מלוח ההקרנות ושארוך ממנו. אם לא נמצא – None.
    """
    short = short_desc.rstrip(". …").strip()
    if len(short) < 20:
        return None
    prefix = short[:40]
    soup = BeautifulSoup(event_html, "html.parser")
    for junk in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        junk.decompose()
    best = None
    for el in soup.find_all(["p", "div", "section", "article"]):
        flat = clean(el.get_text(" "))
        if prefix in flat and len(flat) > len(short) + 5:
            if best is None or len(flat) < len(best[0]):
                best = (flat, el)
    if best is None:
        return None
    el = best[1]
    # אם התיאור מפוצל לכמה פסקאות – לוקחים את כל הפסקאות הסמוכות
    if el.name == "p" and el.parent is not None:
        sibs = el.parent.find_all("p", recursive=False)
        paras = [clean(p.get_text(" ")) for p in sibs]
        text = "\n".join(x for x in paras if x)
        return text if len(text) < 6000 else None
    # שומר מעברי פסקה
    paras = [clean(x) for x in el.get_text("\n").split("\n")]
    text = "\n".join(x for x in paras if x)
    return text if len(text) < 6000 else None


_desc_cache = {}


def fill_full_descriptions(days):
    """מוריד כל עמוד סרט פעם אחת בלבד ומחליף את התיאור המקוצר בתיאור המלא."""
    for d in days:
        for s in d["cin"] or []:
            url = s["info"]
            if url not in _desc_cache:
                full = None
                try:
                    r = requests.get(url, headers=HEADERS, timeout=30)
                    r.raise_for_status()
                    full = extract_full_description(r.text, s["desc"])
                except Exception as e:  # noqa: BLE001
                    print(f"אזהרה: תיאור מלא לא נטען ({s['title']}): {e}", file=sys.stderr)
                _desc_cache[url] = full
                time.sleep(0.3)  # בנימוס כלפי שרת הסינמטק
            if _desc_cache[url]:
                s["desc"] = _desc_cache[url]
    found = sum(1 for v in _desc_cache.values() if v)
    print(f"תיאורים מלאים: {found} מתוך {len(_desc_cache)} סרטים")


# ======== קולנוע לב ========
def parse_lev(page_html, date):
    """
    ניסיון כללי: אתרי קולנוע רבים מטמיעים נתוני JSON-LD מסוג ScreeningEvent.
    אם לא נמצא – מוחזרת רשימה ריקה, והדף יציג קישור לאתר לב.
    (כשנקבל עמוד לדוגמה מאתר לב, נכתוב כאן מפענח מדויק.)
    """
    soup = BeautifulSoup(page_html, "html.parser")
    shows = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            if not isinstance(it, dict) or it.get("@type") not in ("ScreeningEvent", "Event"):
                continue
            start = it.get("startDate", "")
            if not start.startswith(date.isoformat()):
                continue
            work = it.get("workPresented") or {}
            title = work.get("name") if isinstance(work, dict) else None
            shows.append({
                "time": start[11:16],
                "title": clean(title or it.get("name", "")),
                "meta": "", "lang": "", "family": False,
                "link": it.get("url") or LEV_URL, "info": it.get("url") or LEV_URL,
            })
    shows.sort(key=lambda s: s["time"])
    return shows


def fetch_lev(date):
    r = requests.get(LEV_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_lev(r.text, date)


# ======== בניית הדף ========
def day_label(date, today):
    diff = (date - today).days
    name = {0: "היום", 1: "מחר"}.get(diff, HEB_DAYS[date.weekday()])
    return name, f"יום {HEB_DAYS[date.weekday()]}, {date.day}.{date.month}"


def render_shows(shows, fallback_url, fallback_name):
    if shows is None:
        return (f'<p class="empty">לא הצלחנו לטעון את הלוח הפעם.<br>'
                f'<a class="btn" href="{fallback_url}">לאתר {fallback_name}</a></p>')
    if not shows:
        return (f'<p class="empty">אין הקרנות ברשימה.<br>'
                f'<a class="btn" href="{fallback_url}">לבדיקה באתר {fallback_name}</a></p>')
    out = []
    for s in shows:
        details = " · ".join(x for x in [s["meta"], s["lang"]] if x)
        desc = s.get("desc", "")
        director = s.get("director", "")
        if desc or director:
            title_html = (
                '<details><summary class="title">'
                f'{html.escape(s["title"])}<span class="arrow" aria-hidden="true">◀</span></summary>'
                '<div class="desc">'
                + (f'<div class="dir">בימוי: {html.escape(director)}</div>' if director else "")
                + html.escape(desc)
                + f'<a class="more" href="{html.escape(s["info"])}">לעמוד הסרט באתר</a>'
                '</div></details>'
            )
        else:
            title_html = f'<div class="title">{html.escape(s["title"])}</div>'
        out.append(
            f'<li class="show{" fam" if s["family"] else ""}" data-time="{html.escape(s["time"])}">'
            f'<div class="time">{html.escape(s["time"])}</div>'
            f'<div class="body">{title_html}'
            + (f'<div class="meta">{html.escape(details)}</div>' if details else "")
            + (f'<div class="tag">לכל המשפחה</div>' if s["family"] else "")
            + f'<a class="btn" href="{html.escape(s["link"])}">פרטים והזמנה</a></div></li>'
        )
    return '<ul class="shows">' + "".join(out) + "</ul>"


def build_page(days, generated_at):
    tabs, panes = [], []
    for i, d in enumerate(days):
        short, long_ = d["label"]
        tabs.append(f'<button class="tab{" on" if i == 0 else ""}" data-i="{i}">{short}<small>{d["date"].day}.{d["date"].month}</small></button>')
        panes.append(
            f'<section class="day{" on" if i == 0 else ""}" data-i="{i}" data-date="{d["date"].isoformat()}">'
            f'<h2>{long_}</h2>'
            f'<h3 class="venue">🎬 סינמטק תל אביב</h3>'
            + render_shows(d["cin"], CINEMATHEQUE_HOME, "הסינמטק")
            + f'<h3 class="venue">🎬 {LEV_NAME}</h3>'
            + render_shows(d["lev"], LEV_URL, "לב")
            + "</section>"
        )
    return TEMPLATE.replace("{{TABS}}", "".join(tabs)) \
                   .replace("{{PANES}}", "".join(panes)) \
                   .replace("{{UPDATED}}", generated_at.strftime("%d.%m %H:%M"))


TEMPLATE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="סרטים">
<meta name="theme-color" content="#1d3557">
<title>סרטים היום</title>
<style>
  :root { --ink:#111; --muted:#555; --bg:#fffdf8; --accent:#1d3557; --line:#e4ddd0; }
  * { box-sizing:border-box; }
  body { margin:0; font-family: Arial, "Segoe UI", sans-serif; font-size:21px; line-height:1.45;
         color:var(--ink); background:var(--bg); }
  header { position:sticky; top:0; background:var(--accent); color:#fff; padding:10px 10px 12px; z-index:2; }
  header h1 { margin:0 0 8px; font-size:24px; text-align:center; }
  .tabs { display:grid; grid-template-columns:repeat(4, 1fr); gap:6px; }
  .tab { font-size:19px; font-weight:bold; padding:8px 2px; border-radius:12px; border:0;
         background:#fff3; color:#fff; font-family:inherit; }
  .tab small { display:block; font-size:16px; font-weight:normal; }
  .tab.on { background:#fff; color:var(--accent); }
  main { padding:12px 14px 40px; max-width:700px; margin:0 auto; }
  .day { display:none; } .day.on { display:block; }
  h2 { font-size:24px; margin:8px 0 4px; }
  .venue { font-size:23px; margin:26px 0 8px; padding-bottom:6px; border-bottom:3px solid var(--accent); }
  .shows { list-style:none; margin:0; padding:0; }
  .show { display:flex; gap:14px; padding:14px 0; border-bottom:1px solid var(--line); }
  .show .time { font-size:28px; font-weight:bold; min-width:84px; color:var(--accent); }
  .show .title { font-size:23px; font-weight:bold; }
  .show .body { flex:1; min-width:0; }
  summary.title { list-style:none; cursor:pointer; }
  summary.title::-webkit-details-marker { display:none; }
  .arrow { display:inline-block; font-size:15px; color:var(--accent); margin-right:8px;
           padding:2px 7px; border:1.5px solid var(--accent); border-radius:50%;
           vertical-align:middle; transition:transform .2s; }
  details[open] .arrow { transform:rotate(-90deg); }
  .desc { white-space:pre-line; font-size:19px; background:#f3efe6; border-radius:10px;
          padding:10px 12px; margin:8px 0 4px; }
  .desc .dir { font-weight:bold; margin-bottom:4px; }
  .desc .more { display:block; margin-top:8px; color:var(--accent); font-size:17px; }
  .show .meta { color:var(--muted); font-size:18px; margin-top:2px; }
  .show .tag { display:inline-block; font-size:16px; background:#ffe8a3; padding:1px 8px; border-radius:8px; margin-top:4px; }
  .btn { display:inline-block; margin-top:8px; padding:8px 16px; font-size:19px; border-radius:10px;
         background:var(--accent); color:#fff; text-decoration:none; }
  .past { opacity:.38; }
  .empty { color:var(--muted); }
  footer { text-align:center; color:var(--muted); font-size:15px; padding:20px; }
</style>
</head>
<body>
<header><h1>מה מוקרן?</h1><div class="tabs">{{TABS}}</div></header>
<main>{{PANES}}</main>
<footer>עודכן: {{UPDATED}}</footer>
<script>
  document.querySelectorAll('.tab').forEach(function (b) {
    b.onclick = function () {
      var i = b.dataset.i;
      document.querySelectorAll('.tab,.day').forEach(function (el) {
        el.classList.toggle('on', el.dataset.i === i);
      });
      window.scrollTo(0, 0);
    };
  });
  // הקרנות שכבר התחילו היום מוצגות בצבע חיוור
  var now = new Date();
  var today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
  var hm = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
  var sec = document.querySelector('.day[data-date="' + today + '"]');
  if (sec) sec.querySelectorAll('.show').forEach(function (li) {
    if (li.dataset.time && li.dataset.time < hm) li.classList.add('past');
  });
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cinematheque-file", help="קובץ HTML שמור לבדיקה (במקום הורדה)")
    ap.add_argument("--lev-file", help="קובץ HTML שמור של לב לבדיקה")
    ap.add_argument("--out", default="_site")
    ap.add_argument("--no-full-desc", action="store_true", help="בלי הורדת תיאורים מלאים")
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    today = now.date()
    days, problems = [], []
    for k in range(DAYS_AHEAD):
        d = today + dt.timedelta(days=k)
        try:
            cin = (parse_cinematheque(Path(args.cinematheque_file).read_text(encoding="utf-8"))
                   if args.cinematheque_file else fetch_cinematheque(d))
        except Exception as e:  # noqa: BLE001
            problems.append(f"סינמטק {d}: {e}")
            cin = None
        try:
            lev = (parse_lev(Path(args.lev_file).read_text(encoding="utf-8"), d)
                   if args.lev_file else fetch_lev(d))
        except Exception as e:  # noqa: BLE001
            problems.append(f"לב {d}: {e}")
            lev = None
        days.append({"date": d, "label": day_label(d, today), "cin": cin, "lev": lev})
        print(f"{d}: סינמטק {len(cin) if cin is not None else 'שגיאה'} הקרנות, "
              f"לב {len(lev) if lev is not None else 'שגיאה'} הקרנות")

    if not args.cinematheque_file and not args.no_full_desc:
        fill_full_descriptions(days)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(build_page(days, now), encoding="utf-8")
    print(f"נכתב {out / 'index.html'}")
    for p in problems:
        print("אזהרה:", p, file=sys.stderr)
    # נכשל רק אם הסינמטק לא נטען באף יום – כך GitHub ישלח לך מייל התראה
    if all(d["cin"] is None for d in days):
        sys.exit(1)


if __name__ == "__main__":
    main()
