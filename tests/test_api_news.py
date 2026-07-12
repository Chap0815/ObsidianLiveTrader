"""GET /api/news — merged crypto RSS headlines (public, key-less, cached)."""

from functools import partial

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app.main import _parse_feed, app

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item>
    <title>Bitcoin hits &lt;b&gt;new&lt;/b&gt; high</title>
    <link>https://example.com/a</link>
    <pubDate>Wed, 08 Jul 2026 10:00:00 GMT</pubDate>
    <description>&lt;p&gt;Some &lt;i&gt;summary&lt;/i&gt;&lt;/p&gt;</description>
  </item>
  <item>
    <title>Older story</title>
    <link>https://example.com/b</link>
    <pubDate>Tue, 07 Jul 2026 10:00:00 GMT</pubDate>
    <description>x</description>
  </item>
</channel></rss>"""

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom headline</title>
    <link href="https://example.com/atom"/>
    <updated>2026-07-09T12:00:00Z</updated>
    <summary>atom summary</summary>
  </entry>
</feed>"""


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeClient:
    """Stands in for httpx.AsyncClient; maps URL -> xml string or Exception."""

    def __init__(self, by_url, *a, **k):
        self._by_url = by_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        # The monkeypatch swaps the module-global httpx.AsyncClient, so the
        # app's real exchange client (created at lifespan startup) also gets
        # a _FakeClient here; its shutdown path calls aclose().
        return None

    async def get(self, url, **k):
        v = self._by_url.get(url)
        if isinstance(v, Exception):
            raise v
        return _FakeResp(v)


def _patch(monkeypatch, feeds, by_url):
    monkeypatch.setattr(main, "NEWS_FEEDS", feeds)
    monkeypatch.setattr(
        main.httpx, "AsyncClient", partial(_FakeClient, by_url)
    )


# --- pure parser unit tests ------------------------------------------------
def test_parse_feed_rss_strips_html_and_maps_fields():
    items = _parse_feed(RSS_XML, "CoinDesk")
    assert [i["title"] for i in items] == ["Bitcoin hits new high", "Older story"]
    first = items[0]
    assert first["source"] == "CoinDesk"
    assert first["url"] == "https://example.com/a"
    assert "<" not in first["summary"] and "summary" in first["summary"]
    assert first["published"].startswith("2026-07-08")
    assert first["_sort"] > items[1]["_sort"]  # newer sorts higher


def test_parse_feed_atom():
    items = _parse_feed(ATOM_XML, "Decrypt")
    assert len(items) == 1
    assert items[0]["title"] == "Atom headline"
    assert items[0]["url"] == "https://example.com/atom"
    assert items[0]["published"].startswith("2026-07-09")


# --- endpoint tests --------------------------------------------------------
def test_news_merges_and_sorts(monkeypatch):
    feeds = [("A", "http://a"), ("B", "http://b")]
    _patch(monkeypatch, feeds, {"http://a": RSS_XML, "http://b": ATOM_XML})
    with TestClient(app) as client:
        client.app.state.news_cache = None
        r = client.get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    titles = [i["title"] for i in body["items"]]
    # Atom (Jul 09) newest, then RSS Jul 08, then Jul 07
    assert titles == ["Atom headline", "Bitcoin hits new high", "Older story"]
    assert all("_sort" not in i for i in body["items"])


def test_news_per_feed_error_isolated(monkeypatch):
    feeds = [("Good", "http://ok"), ("Dead", "http://dead")]
    _patch(
        monkeypatch,
        feeds,
        {"http://ok": RSS_XML, "http://dead": httpx.ConnectError("boom")},
    )
    with TestClient(app) as client:
        client.app.state.news_cache = None
        r = client.get("/api/news")
    body = r.json()
    assert [i["source"] for i in body["items"]] == ["Good", "Good"]
    assert len(body["errors"]) == 1 and body["errors"][0].startswith("Dead:")


def test_news_malformed_xml_isolated(monkeypatch):
    feeds = [("Junk", "http://junk"), ("Good", "http://ok")]
    _patch(monkeypatch, feeds, {"http://junk": "<<not xml", "http://ok": RSS_XML})
    with TestClient(app) as client:
        client.app.state.news_cache = None
        r = client.get("/api/news")
    body = r.json()
    assert len(body["errors"]) == 1 and body["errors"][0].startswith("Junk:")
    assert len(body["items"]) == 2


def test_news_total_failure_serves_stale_cache(monkeypatch):
    feeds = [("A", "http://a")]
    with TestClient(app) as client:
        # 1) prime the cache with a good payload
        _patch(monkeypatch, feeds, {"http://a": RSS_XML})
        client.app.state.news_cache = None
        first = client.get("/api/news").json()
        assert first["items"]
        # 2) force TTL expiry + a dead feed -> stale fallback, not empty
        ts, payload = client.app.state.news_cache
        client.app.state.news_cache = (ts - main.NEWS_CACHE_TTL - 1, payload)
        _patch(monkeypatch, feeds, {"http://a": httpx.ConnectError("down")})
        r = client.get("/api/news")
    body = r.json()
    assert body.get("stale") is True
    assert body["items"] == first["items"]
