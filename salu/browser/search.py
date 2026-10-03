"""Web search. Primary: DuckDuckGo's HTML endpoint (no API key, no browser needed).
Fallback: Bing through SALU's browser if DuckDuckGo returns nothing or blocks the request."""

from __future__ import annotations

import logging
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger("salu.search")

DDG_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")


class _DdgParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._field: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "a" and "result__a" in cls:
            self.results.append({"title": "", "url": _ddg_target(a.get("href", "")), "snippet": ""})
            self._field = "title"
        elif "result__snippet" in cls and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "td", "div"):
            self._field = None

    def handle_data(self, data):
        if self._field and self.results:
            self.results[-1][self._field] += data


def _ddg_target(href: str) -> str:
    href = unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    q = parse_qs(urlparse(href).query)
    return q.get("uddg", [href])[0]


def parse_ddg(html: str, max_results: int) -> list[dict]:
    p = _DdgParser()
    p.feed(html)
    out, seen = [], set()
    for r in p.results:
        url = r["url"]
        if not url.startswith("http") or "duckduckgo.com/y.js" in url or "ad_domain" in url or url in seen:
            continue  # skip ads and duplicates
        seen.add(url)
        out.append({"title": " ".join(r["title"].split()), "url": url, "snippet": " ".join(r["snippet"].split())})
        if len(out) >= max_results:
            break
    return out


async def ddg_search(query: str, max_results: int = 8) -> list[dict]:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
        r = await c.post(DDG_URL, data={"q": query})
        r.raise_for_status()
        return parse_ddg(r.text, max_results)
