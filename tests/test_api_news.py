"""GET /api/news — merged crypto RSS headlines (public, key-less, cached)."""

from functools import partial

import defusedxml
import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import _parse_feed, _parse_news_date, app

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
        self.encoding = "utf-8"

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        # Chunked to exercise the streaming size-guard (F-14) like a real
        # response instead of handing back one giant chunk.
        data = self.text.encode("utf-8")
        step = 4096
        for i in range(0, len(data), step):
            yield data[i : i + step]


class _FakeStreamCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        if isinstance(self._value, Exception):
            raise self._value
        return _FakeResp(self._value)

    async def __aexit__(self, *a):
        return False


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

    def stream(self, method, url, **k):
        return _FakeStreamCtx(self._by_url.get(url))


def _patch(monkeypatch, feeds, by_url):
    monkeypatch.setattr(main, "NEWS_FEEDS", feeds)
    monkeypatch.setattr(
        main.httpx, "AsyncClient", partial(_FakeClient, by_url)
    )


class _RecordingClient(_FakeClient):
    """Like _FakeClient but records the kwargs the app constructed it with."""

    calls: list[dict] = []

    def __init__(self, by_url, *a, **k):
        super().__init__(by_url, *a, **k)
        _RecordingClient.calls.append(k)


# --- pure parser unit tests ------------------------------------------------
def test_parse_news_date_unparseable_is_html_stripped():
    # An unparseable date must still be tag-stripped: no feed-derived string
    # may reach the payload un-stripped (defence in depth with the frontend).
    display, sort_key = _parse_news_date("<b>not a date</b>")
    assert display == "not a date"
    assert sort_key == 0.0


def test_parse_feed_keeps_other_items_when_date_overflows_utc_conversion():
    xml = """<rss><channel>
      <item><title>Boundary date</title>
        <pubDate>0001-01-01T00:00:00+14:00</pubDate></item>
      <item><title>Valid date</title>
        <pubDate>Wed, 08 Jul 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""

    items = _parse_feed(xml, "Synthetic")

    assert [item["title"] for item in items] == ["Boundary date", "Valid date"]
    assert items[0]["published"] == "0001-01-01T00:00:00+14:00"
    assert items[0]["_sort"] == 0.0
    assert items[1]["_sort"] > 0.0


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


def test_parse_feed_rejects_dtd_entities():
    # Feeds are untrusted network XML: a DTD/entity payload (billion-laughs or
    # XXE vector) must be rejected by the hardened parser, not expanded. The
    # per-feed try/except in news() then isolates it as an error.
    bomb = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE rss [<!ENTITY lol "lol">]>'
        "<rss><channel><item><title>&lol;</title>"
        "<link>https://x/</link></item></channel></rss>"
    )
    with pytest.raises(defusedxml.common.DefusedXmlException):
        _parse_feed(bomb, "Evil")


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


# --- F-13: SSRF — feed redirects must never be followed ---------------------
def test_news_client_does_not_follow_redirects(monkeypatch):
    """A compromised/misconfigured feed could 30x-redirect to loopback/LAN/
    cloud-metadata targets. The news AsyncClient must be built with
    follow_redirects=False so such a response fails isolated instead."""
    feeds = [("A", "http://a")]
    _RecordingClient.calls = []
    monkeypatch.setattr(main, "NEWS_FEEDS", feeds)
    monkeypatch.setattr(
        main.httpx, "AsyncClient", partial(_RecordingClient, {"http://a": RSS_XML})
    )
    with TestClient(app) as client:
        client.app.state.news_cache = None
        r = client.get("/api/news")
    assert r.status_code == 200
    news_calls = [c for c in _RecordingClient.calls if "follow_redirects" in c]
    assert news_calls, "expected the news endpoint to construct an AsyncClient"
    assert news_calls[0].get("follow_redirects") is False


# --- F-14: resource DoS — size cap + singleflight refresh --------------------
def test_news_oversized_feed_is_rejected_isolated(monkeypatch):
    """A feed response larger than the cap must be aborted mid-stream and
    reported as a per-feed error, never parsed/cached whole."""
    huge = "<rss><channel>" + ("<item><title>x</title></item>" * 200_000) + "</channel></rss>"
    assert len(huge.encode("utf-8")) > main.NEWS_MAX_FEED_BYTES
    feeds = [("Huge", "http://huge"), ("Good", "http://ok")]
    _patch(monkeypatch, feeds, {"http://huge": huge, "http://ok": RSS_XML})
    with TestClient(app) as client:
        client.app.state.news_cache = None
        r = client.get("/api/news")
    body = r.json()
    assert len(body["errors"]) == 1 and body["errors"][0].startswith("Huge:")
    # The good feed still comes through — isolated failure, not a crash.
    assert [i["source"] for i in body["items"]] == ["Good", "Good"]


@pytest.mark.asyncio
async def test_concurrent_cache_miss_only_refreshes_once(monkeypatch):
    """Singleflight: two concurrent cache-miss requests must trigger only one
    feed-fetch batch — the second reuses the first's result instead of firing
    its own full fetch storm."""
    import asyncio as _asyncio

    fetch_batches = 0
    started = _asyncio.Event()
    release = _asyncio.Event()

    async def fake_refresh(request):
        nonlocal fetch_batches
        fetch_batches += 1
        started.set()
        await release.wait()
        payload = {"items": [{"title": "one"}], "errors": []}
        request.app.state.news_cache = (main._time.monotonic(), payload)
        return payload

    monkeypatch.setattr(main, "_refresh_news", fake_refresh)
    app.state.news_cache = None
    app.state.news_lock = _asyncio.Lock()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        t1 = _asyncio.create_task(client.get("/api/news"))
        await _asyncio.wait_for(started.wait(), timeout=2.0)
        t2 = _asyncio.create_task(client.get("/api/news"))
        await _asyncio.sleep(0.1)  # let t2 reach (and block on) the lock
        release.set()
        r1 = await t1
        r2 = await t2
    assert r1.status_code == 200 and r2.status_code == 200
    assert fetch_batches == 1


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


# --- Finding 1: cold-start all-feeds-failed must not hide news for 5 min ----
def test_news_cold_start_all_fail_retries_soon_not_full_ttl(monkeypatch):
    """A cold-start refresh (no prior cache) where every feed fails must not
    cache the empty payload for the full NEWS_CACHE_TTL — only for the short
    NEWS_NEGATIVE_CACHE_TTL — so a feed recovering seconds later is retried
    promptly instead of the page staying empty for up to 5 min."""
    feeds = [("A", "http://a")]
    fake_now = [5000.0]
    monkeypatch.setattr(main._time, "monotonic", lambda: fake_now[0])
    with TestClient(app) as client:
        client.app.state.news_cache = None
        _patch(monkeypatch, feeds, {"http://a": httpx.ConnectError("down")})
        first = client.get("/api/news").json()
        assert first["items"] == []

        # Well past NEWS_NEGATIVE_CACHE_TTL but far short of the full
        # NEWS_CACHE_TTL — the old code would still serve the empty cache for
        # up to 5 min; the fix must retry here.
        fake_now[0] += main.NEWS_NEGATIVE_CACHE_TTL + 1
        _patch(monkeypatch, feeds, {"http://a": RSS_XML})
        second = client.get("/api/news").json()
    assert second["items"], "expected a retry well before NEWS_CACHE_TTL elapses"


# --- Finding 2: all-fail stale return must set a short negative-TTL too -----
def test_news_all_fail_stale_reuses_negative_ttl_then_refreshes(monkeypatch):
    """After an all-feeds-failed stale return, news_cache[0] must be updated
    too (short negative TTL): repeat callers within that window hit the TTL
    cache (no second upstream fetch batch each request), and once the window
    elapses a real refresh happens again."""
    fake_now = [2000.0]
    monkeypatch.setattr(main._time, "monotonic", lambda: fake_now[0])
    refresh_calls = 0
    orig_refresh = main._refresh_news

    async def counting_refresh(request):
        nonlocal refresh_calls
        refresh_calls += 1
        return await orig_refresh(request)

    monkeypatch.setattr(main, "_refresh_news", counting_refresh)

    feeds = [("A", "http://a")]
    with TestClient(app) as client:
        # 1) prime a good cache
        _patch(monkeypatch, feeds, {"http://a": RSS_XML})
        client.app.state.news_cache = None
        first = client.get("/api/news").json()
        assert first["items"]
        assert refresh_calls == 1

        # 2) expire the TTL, all feeds now fail -> stale fallback
        ts, payload = client.app.state.news_cache
        client.app.state.news_cache = (ts - main.NEWS_CACHE_TTL - 1, payload)
        _patch(monkeypatch, feeds, {"http://a": httpx.ConnectError("down")})
        r = client.get("/api/news")
        assert refresh_calls == 2
        assert r.json().get("stale") is True

        # 3) still inside the negative TTL -> cache hit, no second fetch batch
        fake_now[0] += main.NEWS_NEGATIVE_CACHE_TTL - 1
        r2 = client.get("/api/news")
        assert refresh_calls == 2, "expected the negative-TTL cache to be hit"
        assert r2.json().get("stale") is True

        # 4) after the negative TTL elapses -> a real refresh happens again
        fake_now[0] = 2021.0
        _patch(monkeypatch, feeds, {"http://a": RSS_XML})
        r3 = client.get("/api/news")
    assert refresh_calls == 3
    assert r3.json()["items"]
