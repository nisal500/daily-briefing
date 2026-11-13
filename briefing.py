#!/usr/bin/env python3
"""Daily briefing -> ntfy. Python 3.9+ standard library only.

Modes:
  daily    Schedule today's briefing for 7:30 AM Vancouver (used by the cron).
  test     Send a TEST briefing right now (does not count as the daily one).
  dry-run  Build and print the briefing. Sends nothing.

Env vars:
  NTFY_TOPIC  (required for daily/test)  your topic name - keep it in a GitHub Secret
  NTFY_SERVER (optional) default https://ntfy.sh
  NTFY_TOKEN  (optional) access token, only if you later protect the topic
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

def _vancouver_tz():
    """B.C. uses permanent Pacific Time (UTC-7) since Nov 1, 2026.
    Older servers may have outdated timezone data that still says UTC-8 in winter.
    If so, fall back to a fixed UTC-7 so the briefing is never an hour late."""
    z = ZoneInfo("America/Vancouver")
    if datetime(2026, 12, 15, 12, tzinfo=z).utcoffset() != timedelta(hours=-7):
        print("WARNING: outdated tzdata on this machine; using fixed UTC-7", flush=True)
        return timezone(timedelta(hours=-7), "PT")
    return z


TZ = _vancouver_tz()
SEND_HOUR, SEND_MIN = 7, 30
LATE_CUTOFF_HOUR = 11          # after this, skip instead of sending a stale briefing
NEWS_MAX_AGE_H = 30            # stories older than this are ignored
MAX_WORDS = 380                # ~2 min reading time
MAX_BYTES = 3800               # ntfy turns messages > 4096 bytes into attachments
UA = "DailyBriefing/1.0 (personal, non-commercial)"

LAT, LON = 49.2827, -123.1207
OPEN_METEO = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=temperature_2m,precipitation_probability"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=America%2FVancouver&forecast_days=1"
).format(lat=LAT, lon=LON)
ECCC_FEED = "https://weather.gc.ca/rss/weather/49.245_-123.115_e.xml"

# category -> feeds (feed order = editorial importance)
FEEDS = {
    "world":    ["https://feeds.bbci.co.uk/news/world/rss.xml"],
    "canada":   ["https://www.cbc.ca/webfeed/rss/rss-topstories",
                 "https://www.cbc.ca/webfeed/rss/rss-canada"],
    "business": ["https://www.cbc.ca/webfeed/rss/rss-business",
                 "https://feeds.bbci.co.uk/news/business/rss.xml"],
    "tech":     ["https://feeds.bbci.co.uk/news/technology/rss.xml",
                 "https://www.cbc.ca/webfeed/rss/rss-technology"],
}
CATEGORY_ORDER = ["world", "canada", "business", "tech"]

IMPORTANT = re.compile(
    r"\b(econom\w*|inflation|interest rates?|bank of canada|federal reserve|fed\b|"
    r"tariffs?|trade|jobs?|unemployment|recession|gdp|budget|tax\w*|housing|"
    r"mortgages?|rent|prices?|cost of living|markets?|stocks?|oil|gas prices|"
    r"loonie|dollar|wages?|pensions?|ai\b|artificial intelligence|cyber\w*|"
    r"data breach|chips?|semiconductor\w*|regulation|strike|supply)", re.I)
BLOCK = re.compile(
    r"\b(celebrit\w*|kardashian|red carpet|oscars?|grammys?|emmys?|box office|"
    r"reality (tv|show)|horoscope|quiz|recipe|royal family|influencer|"
    r"nhl|nba|nfl|mlb|fifa|world cup|canucks|soccer|football|hockey|tennis|golf|"
    r"olympic\w*)\b|^(watch|in pictures|listen|video|live)\b[:|]", re.I)

HABITS = [
    "Block 90 minutes for your most important work before checking messages.",
    "Learn one new skill for 20 minutes (a course, book chapter or tutorial).",
    "Review this week's spending and cancel one subscription you don't use.",
    "Take a 20-minute walk without your phone to reset your focus.",
    "Write down your top 3 goals for the week and pick one small next step.",
    "Tidy your workspace and inbox folders for 15 minutes.",
    "Drink water first, then plan meals so you avoid last-minute takeout.",
    "Reach out to one friend or colleague you haven't talked to recently.",
    "Read 15 pages of a non-fiction book in an area you want to grow.",
    "Check that your passwords and backups are up to date for one account.",
    "Stop screens 30 minutes before bed tonight to sleep better.",
    "Spend 10 minutes updating your resume or LinkedIn with recent wins.",
    "Do a 10-minute stretch or bodyweight workout.",
    "Say no to one low-value task so you have time for what matters.",
]


def log(msg):
    """Logs go to stdout (GitHub Actions log). Never log the topic or tokens."""
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- network
def http(url, data=None, headers=None, method=None, retries=3, timeout=20):
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network errors, timeouts, HTTP 5xx...
            last = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"request failed after {retries} tries: {type(last).__name__}")


def safe_host(url):
    return urllib.parse.urlsplit(url).netloc


# ---------------------------------------------------------------- news
def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def first_sentence(s, limit=160):
    s = clean_text(s)
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    if m and len(m.group(1)) >= 30:
        s = m.group(1)
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s


def clean_link(url):
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def parse_rss(xml_bytes, now_utc):
    """Return fresh items: dicts with title, summary, link, published, pos."""
    root = ET.fromstring(xml_bytes)
    items = []
    for pos, it in enumerate(root.iter("item")):
        title = clean_text(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        if not (title and link and pub):
            continue  # can't verify freshness/source -> skip
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except Exception:
            continue
        age_h = (now_utc - published).total_seconds() / 3600
        if age_h > NEWS_MAX_AGE_H or age_h < -1:
            continue
        items.append({
            "title": title,
            "summary": first_sentence(it.findtext("description")),
            "link": clean_link(link),
            "published": published,
            "pos": pos,
        })
    return items


def score(item):
    text = f"{item['title']} {item['summary']}"
    s = 10 - min(item["pos"], 10)
    s += min(len(set(m.lower() for m in IMPORTANT.findall(text))), 2) * 4
    if item["title"].endswith("?"):
        s -= 3  # likely clickbait / explainer
    return s


def words(s):
    return set(re.findall(r"[a-z0-9]{4,}", s.lower()))


def is_dup(item, chosen):
    w = words(item["title"])
    for c in chosen:
        cw = words(c["title"])
        if w and cw and len(w & cw) / len(w | cw) >= 0.35:
            return True
        if item["link"] == c["link"]:
            return True
    return False


def select_news(by_cat, max_items=5):
    chosen = []
    leftovers = []
    for cat in CATEGORY_ORDER:
        cands = [i for i in by_cat.get(cat, [])
                 if not BLOCK.search(i["title"]) and not BLOCK.search(i["summary"])]
        cands.sort(key=score, reverse=True)
        picked = False
        for c in cands:
            if not picked and not is_dup(c, chosen):
                chosen.append(c)
                picked = True
            else:
                leftovers.append(c)
    # optional 5th story: only if clearly about money/economy/tech/everyday life
    leftovers.sort(key=score, reverse=True)
    for c in leftovers:
        if len(chosen) >= max_items:
            break
        if IMPORTANT.search(c["title"]) and not is_dup(c, chosen):
            chosen.append(c)
            break
    return chosen[:max_items]


def fetch_news(now_utc):
    by_cat = {}
    for cat, urls in FEEDS.items():
        by_cat[cat] = []
        for url in urls:
            try:
                items = parse_rss(http(url), now_utc)
                for i in items:
                    i["category"] = cat
                by_cat[cat].extend(items)
                log(f"news ok: {safe_host(url)} ({cat}) {len(items)} fresh")
            except Exception as e:
                log(f"news FAILED: {safe_host(url)} ({cat}): {e}")
    return select_news(by_cat)


# ---------------------------------------------------------------- weather
def parse_open_meteo(data, today, at_local):
    d = data["daily"]
    if d["time"][0] != today.isoformat():
        raise ValueError("forecast is not for today")
    hours = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    pops = data["hourly"]["precipitation_probability"]

    def at(hh):
        key = f"{today.isoformat()}T{hh:02d}:00"
        return hours.index(key) if key in hours else None

    i0, i1 = at(at_local.hour), at(min(at_local.hour + 1, 23))
    temp = None
    if i0 is not None and i1 is not None:
        frac = at_local.minute / 60
        temp = temps[i0] + (temps[i1] - temps[i0]) * frac
    # wettest hour during the day (6 AM - 10 PM)
    day = [(pops[i], int(h[11:13])) for i, h in enumerate(hours)
           if 6 <= int(h[11:13]) <= 22 and pops[i] is not None]
    peak = max(day, key=lambda x: (x[0], -x[1])) if day else None
    return {
        "temp": temp,
        "high": d["temperature_2m_max"][0],
        "low": d["temperature_2m_min"][0],
        "pop": d["precipitation_probability_max"][0],
        "peak_hour": peak[1] if peak and peak[0] >= 40 else None,
    }


def parse_eccc(xml_bytes, now_utc):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_bytes)
    updated = datetime.fromisoformat(root.findtext("a:updated", namespaces=ns).replace("Z", "+00:00"))
    if (now_utc - updated) > timedelta(hours=6):
        raise ValueError("ECCC feed is stale")
    warnings, observed = [], None
    for e in root.findall("a:entry", ns):
        cat = e.find("a:category", ns)
        term = cat.get("term") if cat is not None else ""
        title = clean_text(e.findtext("a:title", namespaces=ns))
        if term == "Warnings and Watches":
            low = title.lower()
            if low.startswith("no watches or warnings") or " ended" in low:
                continue
            warnings.append(re.sub(r",\s*Vancouver$", "", title).title())
        elif term == "Current Conditions":
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*°C", title)
            if m:
                observed = float(m.group(1))
    return {"warnings": warnings, "observed": observed}


def fetch_weather(now_local, at_local):
    w = {"ok": False, "warnings": None}
    try:
        data = json.loads(http(OPEN_METEO))
        w.update(parse_open_meteo(data, now_local.date(), at_local))
        w["ok"] = True
        log("weather ok: open-meteo")
    except Exception as e:
        log(f"weather FAILED: open-meteo: {e}")
    try:
        ec = parse_eccc(http(ECCC_FEED), now_local.astimezone(timezone.utc))
        w["warnings"] = ec["warnings"]
        if w.get("temp") is None and ec["observed"] is not None:
            w["temp"], w["temp_label"] = ec["observed"], f"observed {now_local:%-I:%M %p}"
        log(f"weather ok: eccc ({len(ec['warnings'])} alerts)")
    except Exception as e:
        log(f"weather FAILED: eccc: {e}")
    return w


# ---------------------------------------------------------------- priorities
def short(s, n=55):
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def build_priorities(news, weather, day):
    p = []
    money = next((n for n in news if n["category"] == "business" or
                  re.search(r"econom|inflation|rate|tariff|price|housing|job|budget|tax|market",
                            n["title"], re.I)), None)
    tech = next((n for n in news if n["category"] == "tech"), None)
    if money:
        p.append(f"Money check: read \"{short(money['title'])}\" and note one way it could "
                 f"affect your budget, job or savings.")
    elif tech:
        p.append(f"Stay current: read \"{short(tech['title'])}\" and think how it could "
                 f"change your work or tools.")
    else:
        p.append("Spend 10 minutes reviewing your budget for the month ahead.")

    if weather.get("warnings"):
        p.append("A weather alert is in effect: check weather.gc.ca before you travel "
                 "and allow extra time.")
    elif weather.get("ok") and weather.get("pop") is not None:
        if weather["pop"] >= 60:
            when = f" (wettest around {fmt_hour(weather['peak_hour'])})" if weather.get("peak_hour") is not None else ""
            p.append(f"Rain is likely{when}: plan errands around it and pack a rain jacket.")
        else:
            p.append("Low rain chance: get 20+ minutes outside for a walk or errand.")
    else:
        p.append("Get 20 minutes of movement today, indoors or out.")

    p.append(HABITS[day.timetuple().tm_yday % len(HABITS)])
    return p[:3]


# ---------------------------------------------------------------- message
def fmt_hour(h):
    return datetime(2000, 1, 1, h).strftime("%-I %p")


def fmt_temp(t):
    return "N/A" if t is None else f"{round(t)}°C"


def build_message(news, weather, priorities, day, at_local):
    lines = ["GOOD MORNING! ☀️", day.strftime("%A, %B %-d, %Y"), "", "🌍 TOP NEWS"]
    if news:
        for i, n in enumerate(news, 1):
            lines.append(f"{i}. {n['title']} – {n['summary']}")
            lines.append(f"   {n['link']}")
    else:
        lines.append("News unavailable right now. See cbc.ca/news or bbc.com/news.")

    lines += ["", "🌦 VANCOUVER WEATHER"]
    if weather.get("ok") or weather.get("temp") is not None:
        label = weather.get("temp_label") or f"forecast {at_local:%-I:%M %p}"
        lines.append(f"Temperature: {fmt_temp(weather.get('temp'))} ({label})")
        if weather.get("ok"):
            lines.append(f"High / Low: {fmt_temp(weather['high'])} / {fmt_temp(weather['low'])}")
            rain = f"{weather['pop']}%" if weather.get("pop") is not None else "N/A"
            if weather.get("peak_hour") is not None:
                rain += f" (most likely around {fmt_hour(weather['peak_hour'])})"
            lines.append(f"Rain: {rain}")
        else:
            lines.append("High / Low: unavailable")
            lines.append("Rain: unavailable")
    else:
        lines.append("Weather data unavailable. See weather.gc.ca.")
    if weather.get("warnings") is None:
        lines.append("Alerts: unavailable – check weather.gc.ca")
    elif weather["warnings"]:
        lines.append("⚠️ Alerts: " + "; ".join(weather["warnings"][:2]))
    else:
        lines.append("Alerts: none in effect")

    lines += ["", "🎯 TODAY'S PRIORITIES"]
    lines += [f"{i}. {p}" for i, p in enumerate(priorities, 1)]
    lines += ["", "Have a productive day!"]
    return "\n".join(lines)


def fit(news, weather, priorities, day, at_local):
    """Keep under ~2 min reading time and under ntfy's size limit."""
    news = list(news)
    while True:
        msg = build_message(news, weather, priorities, day, at_local)
        n_words = len(re.findall(r"[A-Za-z0-9']+", re.sub(r"https?://\S+", "", msg)))
        if (n_words <= MAX_WORDS and len(msg.encode()) <= MAX_BYTES) or len(news) <= 3:
            return msg
        news = news[:-1]


# ---------------------------------------------------------------- ntfy
def ntfy_headers():
    h = {}
    tok = os.environ.get("NTFY_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def already_sent(server, topic, title):
    """True if today's briefing was already delivered OR is scheduled. Raises on error."""
    url = f"{server}/{urllib.parse.quote(topic)}/json?poll=1&sched=1&since=24h"
    body = http(url, headers=ntfy_headers()).decode("utf-8", "replace")
    for line in body.splitlines():
        if not line.strip():
            continue
        m = json.loads(line)
        if m.get("event") == "message" and m.get("title") == title:
            return True
    return False


def publish(server, topic, title, message, deliver_at=None):
    payload = {"topic": topic, "title": title, "message": message,
               "tags": ["sunrise"], "priority": 3}
    if deliver_at is not None:
        payload["delay"] = str(int(deliver_at.timestamp()))
    resp = http(server + "/", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", **ntfy_headers()},
                method="POST", retries=3)
    out = json.loads(resp)
    if not out.get("id"):
        raise RuntimeError("ntfy did not return a message id")
    return out


def plan_delivery(now_local):
    """Return ('schedule', dt) / ('now', None) / ('skip', reason)."""
    target = now_local.replace(hour=SEND_HOUR, minute=SEND_MIN, second=0, microsecond=0)
    if now_local < target - timedelta(seconds=15):
        return "schedule", target
    if now_local.hour < LATE_CUTOFF_HOUR:
        return "now", None
    return "skip", f"too late ({now_local:%H:%M}); cutoff {LATE_CUTOFF_HOUR}:00"


def daily_title(day):
    return f"Daily Briefing – {day:%a %b %-d, %Y}"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "test", "dry-run"], default="dry-run")
    args = ap.parse_args()

    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if args.mode != "dry-run" and not topic:
        log("ERROR: NTFY_TOPIC is not set")
        return 2

    now = datetime.now(TZ)
    day = now.date()
    title = daily_title(day)
    log(f"mode={args.mode} local_time={now:%Y-%m-%d %H:%M} Vancouver")

    action, deliver_at = ("now", None)
    if args.mode == "daily":
        action, deliver_at = plan_delivery(now)
        if action == "skip":
            log(f"skip: {deliver_at}")
            return 0
        try:
            if already_sent(server, topic, title):
                log("skip: today's briefing is already sent or scheduled")
                return 0
        except Exception as e:
            # Can't confirm -> don't risk a duplicate. A later backup run will retry.
            log(f"ERROR: duplicate check failed ({e}); not sending")
            return 1

    at_local = deliver_at or now
    news = fetch_news(now.astimezone(timezone.utc))
    weather = fetch_weather(now, at_local)
    priorities = build_priorities(news, weather, day)
    message = fit(news, weather, priorities, day, at_local)
    log(f"built: {len(news)} stories, weather_ok={weather.get('ok')}, {len(message.encode())} bytes")

    if args.mode == "dry-run":
        print("\n----- TITLE -----\n" + title + "\n----- MESSAGE -----\n" + message)
        return 0
    if args.mode == "test":
        title = "TEST – " + title

    try:
        out = publish(server, topic, title, message, deliver_at if action == "schedule" else None)
    except Exception as e:
        log(f"ERROR: publish failed: {e}")
        return 1
    when = f"scheduled for {deliver_at:%H:%M} Vancouver" if action == "schedule" else "sent now"
    log(f"OK: ntfy accepted message ({when}), id={out['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
