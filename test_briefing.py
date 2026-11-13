import json, os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
from datetime import datetime, timedelta, timezone
from unittest import mock
sys.path.insert(0, os.path.dirname(HERE))
import briefing as b

TZ = b.TZ
NOW = datetime(2026, 11, 13, 5, 10, tzinfo=TZ)          # a scheduled cron run (PST)
NOW_UTC = NOW.astimezone(timezone.utc)

def rfc(dt): return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

def rss(items):
    x = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description>"
                f"<pubDate>{rfc(p)}</pubDate></item>" for t, l, d, p in items)
    return f"<rss><channel>{x}</channel></rss>".encode()

H = lambda h: NOW_UTC - timedelta(hours=h)
FEEDS = {
 "https://feeds.bbci.co.uk/news/world/rss.xml": rss([
   ("Watch: Crowds gather at festival", "https://bbc.co.uk/v1", "Video.", H(1)),
   ("G20 leaders agree new climate finance deal", "https://www.bbc.com/news/a1?at_medium=RSS", "Leaders agreed a framework to fund climate projects in poorer nations. More text here.", H(3)),
   ("Old story from last week", "https://bbc.co.uk/old", "Stale.", H(100))]),
 "https://www.cbc.ca/webfeed/rss/rss-topstories": rss([
   ("Kardashian celebrity spotted in Toronto", "https://cbc.ca/c0", "Gossip.", H(1)),
   ("Bank of Canada signals pause on interest rates", "https://www.cbc.ca/news/business/boc-1.2?cmp=rss", "&lt;img src='x'/&gt;&lt;p&gt;The central bank said inflation is cooling but risks remain.&lt;/p&gt;", H(2))]),
 "https://www.cbc.ca/webfeed/rss/rss-canada": rss([
   ("B.C. expands rental housing program", "https://www.cbc.ca/news/canada/bc-1.3", "The province will fund 5,000 new rental units.", H(5))]),
 "https://www.cbc.ca/webfeed/rss/rss-business": rss([
   ("Bank of Canada signals a pause on rates", "https://www.cbc.ca/news/business/boc-dup", "Duplicate story.", H(2)),
   ("Canadian gas prices fall ahead of winter", "https://www.cbc.ca/news/business/gas-1.4", "Prices at the pump dropped 6 cents a litre this week.", H(6))]),
 "https://feeds.bbci.co.uk/news/business/rss.xml": rss([]),
 "https://feeds.bbci.co.uk/news/technology/rss.xml": rss([
   ("EU fines tech giant over AI data rules", "https://www.bbc.com/news/tech-5", "Regulators said the firm trained AI models on user data without consent.", H(4)),
   ("NHL star launches gaming app", "https://bbc.co.uk/x", "Sports.", H(1))]),
}
ECCC = open(os.path.join(HERE, "eccc_sample.xml"), "rb").read().replace(b"<updated>2026-11-13T17:13:25Z</updated>", b"<updated>2026-11-13T12:50:00Z</updated>", 1)
hours = [f"2026-11-13T{h:02d}:00" for h in range(24)]
METEO = json.dumps({
  "hourly": {"time": hours, "temperature_2m": [8+h*0.3 for h in range(24)],
             "precipitation_probability": [10]*12 + [85]*4 + [40]*8},
  "daily": {"time": ["2026-11-13"], "temperature_2m_max": [14.2], "temperature_2m_min": [8.0],
            "precipitation_probability_max": [85]}}).encode()

def fake_http(url, **kw):
    if url in FEEDS: return FEEDS[url]
    if url == b.ECCC_FEED: return ECCC
    if url == b.OPEN_METEO: return METEO
    if "cbc.ca/webfeed/rss/rss-technology" in url: raise RuntimeError("timeout")
    raise AssertionError("unexpected url " + url)

class T(unittest.TestCase):
    def build(self):
        with mock.patch.object(b, "http", side_effect=fake_http):
            news = b.fetch_news(NOW_UTC)
            target = NOW.replace(hour=7, minute=30)
            w = b.fetch_weather(NOW, target)
        pr = b.build_priorities(news, w, NOW.date())
        return news, w, pr, b.fit(news, w, pr, NOW.date(), target)

    def test_news_selection(self):
        news, *_ = self.build()
        titles = [n["title"] for n in news]
        print("\nSelected:", *titles, sep="\n  ")
        self.assertTrue(3 <= len(news) <= 5)
        self.assertFalse(any("Kardashian" in t or "Watch" in t or "NHL" in t or "Old" in t for t in titles))
        self.assertEqual(sum("Bank of Canada" in t for t in titles), 1)   # deduped
        self.assertTrue(all("?" not in n["link"] for n in news))           # tracking removed
        self.assertNotIn("<", " ".join(n["summary"] for n in news))

    def test_weather(self):
        _, w, *_ = self.build()
        self.assertEqual(w["high"], 14.2); self.assertEqual(w["pop"], 85)
        self.assertAlmostEqual(w["temp"], 8 + 7.5*0.3, places=1)
        self.assertEqual(w["peak_hour"], 12)
        self.assertEqual(w["warnings"], ["Coastal Flooding Statement"])

    def test_message(self):
        news, w, pr, msg = self.build()
        print("\n" + msg)
        self.assertTrue(msg.startswith("GOOD MORNING! ☀️\nFriday, November 13, 2026"))
        for s in ["🌍 TOP NEWS", "🌦 VANCOUVER WEATHER", "🎯 TODAY'S PRIORITIES", "Have a productive day!", "Rain: 85%"]:
            self.assertIn(s, msg)
        self.assertEqual(len(pr), 3)
        self.assertLess(len(msg.encode()), 3800)
        import re
        self.assertLessEqual(len(re.findall(r"[A-Za-z']+", re.sub(r"https?://\S+", "", msg))), b.MAX_WORDS)

    def test_all_sources_down(self):
        with mock.patch.object(b, "http", side_effect=RuntimeError("offline")):
            news = b.fetch_news(NOW_UTC); w = b.fetch_weather(NOW, NOW)
        msg = b.build_message(news, w, b.build_priorities(news, w, NOW.date()), NOW.date(), NOW)
        self.assertIn("News unavailable", msg); self.assertIn("Weather data unavailable", msg)
        self.assertIn("Alerts: unavailable", msg)

    def test_stale_weather_rejected(self):
        bad = json.loads(METEO); bad["daily"]["time"] = ["2026-11-12"]
        with self.assertRaises(ValueError): b.parse_open_meteo(bad, NOW.date(), NOW)

    def test_plan_delivery(self):
        d = lambda h, m: datetime(2026, 11, 13, h, m, tzinfo=TZ)
        a, t = b.plan_delivery(d(5, 10)); self.assertEqual(a, "schedule"); self.assertEqual((t.hour, t.minute), (7, 30))
        self.assertEqual(b.plan_delivery(d(7, 45))[0], "now")
        self.assertEqual(b.plan_delivery(d(11, 5))[0], "skip")
        # all three cron times (12:10/13:10/14:10 UTC) land before 7:30 PT, summer and winter
        for dt in [datetime(2027, 7, 1, h, 10, tzinfo=timezone.utc) for h in (12, 13, 14)] + \
                  [datetime(2027, 1, 1, h, 10, tzinfo=timezone.utc) for h in (12, 13, 14)]:
            self.assertEqual(b.plan_delivery(dt.astimezone(TZ))[0], "schedule")

    def test_duplicate_guard(self):
        title = b.daily_title(NOW.date())
        cached = json.dumps({"event": "message", "title": title}).encode()
        with mock.patch.object(b, "http", return_value=b'{"event":"open"}\n' + cached):
            self.assertTrue(b.already_sent("https://ntfy.sh", "t", title))
        with mock.patch.object(b, "http", return_value=json.dumps({"event": "message", "title": "TEST – " + title}).encode()):
            self.assertFalse(b.already_sent("https://ntfy.sh", "t", title))   # test msgs don't block daily

    def test_daily_flow_no_duplicate_and_fails_safe(self):
        sent = []
        def pub(*a, **k): sent.append(a); return {"id": "x"}
        with mock.patch.dict("os.environ", {"NTFY_TOPIC": "t"}), \
             mock.patch.object(b, "datetime", wraps=datetime) as dtm, \
             mock.patch.object(b, "publish", side_effect=pub), \
             mock.patch.object(b, "http", side_effect=fake_http), \
             mock.patch.object(sys, "argv", ["x", "--mode", "daily"]):
            dtm.now.return_value = NOW
            with mock.patch.object(b, "already_sent", return_value=True):
                self.assertEqual(b.main(), 0); self.assertEqual(sent, [])
            with mock.patch.object(b, "already_sent", side_effect=RuntimeError("down")):
                self.assertEqual(b.main(), 1); self.assertEqual(sent, [])
            with mock.patch.object(b, "already_sent", return_value=False):
                self.assertEqual(b.main(), 0); self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][4].hour, 7)   # deliver_at 7:30

    def test_publish_payload(self):
        cap = {}
        def h(url, data=None, **k): cap["url"]=url; cap["body"]=json.loads(data); return b'{"id":"abc"}'
        with mock.patch.object(b, "http", side_effect=h):
            b.publish("https://ntfy.sh", "topic", "T", "M", datetime(2026, 11, 13, 7, 30, tzinfo=TZ))
        self.assertEqual(cap["url"], "https://ntfy.sh/")
        self.assertEqual(cap["body"]["delay"], str(int(datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc).timestamp())))


class TZGuard(unittest.TestCase):
    def test_offset_is_utc_minus_7_in_winter(self):
        self.assertEqual(datetime(2027, 1, 15, 12, tzinfo=b.TZ).utcoffset(), timedelta(hours=-7))
    def test_fallback_when_tzdata_outdated(self):
        old = mock.Mock(return_value=timezone(timedelta(hours=-8)))
        with mock.patch.object(b, "ZoneInfo", old):
            tz = b._vancouver_tz()
        self.assertEqual(datetime(2027, 1, 15, tzinfo=tz).utcoffset(), timedelta(hours=-7))

if __name__ == "__main__": unittest.main(verbosity=2)
