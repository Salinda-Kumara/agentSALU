"""SALU's automated browser (Playwright driving installed Edge/Chrome).

One persistent browser context with SALU's own profile (not your everyday
browser profile). Pages are described to Claude as text plus a numbered list
of interactive elements ("refs"); actions target those refs. Every action's
risk is judged from the element as seen in the latest snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

log = logging.getLogger("salu.browser")

SNAPSHOT_JS = r"""
(maxEls) => {
  const vis = el => { const r = el.getBoundingClientRect(); if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el); return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  document.querySelectorAll('[data-salu-ref]').forEach(e => e.removeAttribute('data-salu-ref'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=checkbox],' +
              '[role=tab],[role=menuitem],[role=option],[role=switch],[role=combobox],[contenteditable=true],summary';
  const els = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (n >= maxEls) break;
    if (!vis(el)) continue;
    n++; el.setAttribute('data-salu-ref', String(n));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : '')).toLowerCase();
    const label = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    let name = el.getAttribute('aria-label') || label || (el.innerText || '').trim() || el.getAttribute('placeholder') ||
               el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('name') ||
               ((tag === 'input' && ['submit', 'button'].includes(type)) ? el.value : '') || '';
    const form = el.form || el.closest('form');
    els.push({ref: n, tag, type, role: el.getAttribute('role') || '', name: name.replace(/\s+/g, ' ').trim().slice(0, 100),
      href: tag === 'a' ? el.href : '',
      value: ((tag === 'input' || tag === 'textarea') && type !== 'password') ? String(el.value || '').slice(0, 60) : '',
      checked: (type === 'checkbox' || type === 'radio') ? !!el.checked : null,
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      inForm: !!form, formAction: form ? (form.getAttribute('action') || '') : '',
      formRole: form ? [form.getAttribute('role'), form.id, form.className].join(' ') : '',
      disabled: !!el.disabled});
  }
  const root = document.querySelector('article') || document.querySelector('main,[role=main]') || document.body;
  const text = (root ? root.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n\s*\n+/g, '\n\n').trim();
  return {title: document.title, url: location.href, text, elements: els,
          hasPassword: !!document.querySelector('input[type=password]'),
          hasPayment: !!document.querySelector('input[autocomplete^="cc-"],input[name*="cardnumber" i],input[id*="card-number" i]')};
}
"""

BING_JS = r"""
() => Array.from(document.querySelectorAll('li.b_algo')).map(li => {
  const a = li.querySelector('h2 a'); const p = li.querySelector('.b_caption p, p');
  return a ? {title: a.innerText.trim(), url: a.href, snippet: p ? p.innerText.trim() : ''} : null;
}).filter(Boolean)
"""


def find_browser_executable(channel: str) -> str | None:
    if channel == "chromium":
        return "bundled"
    names = {"msedge": "Microsoft/Edge/Application/msedge.exe", "chrome": "Google/Chrome/Application/chrome.exe"}
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base and (Path(base) / names[channel]).is_file():
            return str(Path(base) / names[channel])
    return None


class BrowserError(Exception):
    pass


class BrowserManager:
    def __init__(self):
        self.settings = None
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self.elements: dict[int, dict[str, Any]] = {}
        self.flags: dict[str, bool] = {}
        self.snapshot_url: str = ""
        self.downloads: list[str] = []

    def configure(self, settings) -> None:
        self.settings = settings

    @property
    def running(self) -> bool:
        return self._ctx is not None

    @property
    def salu_port(self) -> int | None:
        return self.settings.server.port if self.settings else None

    # ---- lifecycle ---------------------------------------------------------------------
    async def _ensure(self):
        if self._ctx is not None:
            await self._drop_internal_pages()
            if self._page is None or self._page.is_closed():
                live = [p for p in self._ctx.pages if not p.is_closed()]
                self._page = live[-1] if live else await self._ctx.new_page()
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BrowserError("Playwright is not installed (pip install playwright).") from e
        cfg = self.settings.browser
        profile = self.settings.path(cfg.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        kwargs = dict(user_data_dir=str(profile), headless=cfg.headless, accept_downloads=True,
                      no_viewport=not cfg.headless, args=["--start-maximized"] if not cfg.headless else [])
        if cfg.channel != "chromium":
            kwargs["channel"] = cfg.channel
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            await self._pw.stop()
            self._pw = None
            msg = str(e).splitlines()[0]
            if "ProcessSingleton" in str(e) or "already in use" in str(e):
                msg = "SALU's browser profile is already open in another window — close that browser and retry."
            raise BrowserError(f"Could not start the browser ({cfg.channel}): {msg}") from e
        self._ctx.on("close", lambda _: self._on_closed())
        self._ctx.on("page", self._on_new_page)
        for p in self._ctx.pages:
            self._watch(p)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        log.info("browser started channel=%s headless=%s", cfg.channel, cfg.headless)
        return self._page

    def _on_closed(self):
        log.info("browser closed")
        self._ctx = self._page = None
        self.elements = {}

    def _on_new_page(self, page):
        self._watch(page)
        self._page = page  # new tab/popup opened by an action becomes the active page

    async def _drop_internal_pages(self):
        """Never act on browser-internal tabs (e.g. Edge's downloads hub that opens on a download).
        They are left open (closing them can hang) but the active page moves back to a real web page."""
        if self._page is not None and not self._page.is_closed() and not _is_internal(self._page.url):
            return
        web = [p for p in self._ctx.pages if not p.is_closed() and not _is_internal(p.url)]
        self._page = web[-1] if web else None

    def _watch(self, page):
        page.on("download", lambda d: asyncio.ensure_future(self._save_download(d)))

    async def _save_download(self, download):
        folder = Path(os.path.expanduser(self.settings.browser.downloads_dir))
        folder.mkdir(parents=True, exist_ok=True)
        name = Path(download.suggested_filename).name or "download"
        target = folder / name
        i = 1
        while target.exists():
            target = folder / f"{Path(name).stem} ({i}){Path(name).suffix}"
            i += 1
        try:
            await download.save_as(str(target))
            self.downloads.append(str(target))
            log.info("download saved %s", target)
        except Exception as e:
            log.warning("download failed: %s", e)

    async def close(self) -> None:
        async with self._lock:
            ctx, pw = self._ctx, self._pw
            self._ctx = self._page = self._pw = None
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass

    # ---- page state ------------------------------------------------------------------------
    async def snapshot(self, max_elements: int = 120) -> dict:
        page = await self._ensure()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        snap = await page.evaluate(SNAPSHOT_JS, max_elements)
        self.elements = {e["ref"]: e for e in snap["elements"]}
        self.flags = {"hasPassword": snap["hasPassword"], "hasPayment": snap["hasPayment"]}
        self.snapshot_url = snap["url"]
        snap["tabs"] = len([p for p in self._ctx.pages if not p.is_closed()])
        return snap

    def element(self, ref: int) -> dict | None:
        return self.elements.get(int(ref))

    async def _locator(self, ref: int):
        page = await self._ensure()
        loc = page.locator(f'[data-salu-ref="{int(ref)}"]')
        if await loc.count() != 1:
            raise BrowserError(f"Element [{ref}] is no longer on the page. Take a new snapshot.")
        return loc

    async def _settle(self, page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except Exception:
            pass
        await asyncio.sleep(0.6)

    # ---- actions -----------------------------------------------------------------------------
    async def goto(self, url: str, new_tab: bool = False) -> dict:
        async with self._lock:
            page = await self._ensure()
            if new_tab:
                page = await self._ctx.new_page()
                self._page = page
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                raise BrowserError(f"Navigation to {url} failed: {str(e).splitlines()[0]}") from e
            await self._settle(page)
            return await self.snapshot()

    async def click(self, ref: int) -> dict:
        async with self._lock:
            loc = await self._locator(ref)
            before = len(self.downloads)
            await loc.click(timeout=10_000)
            await self._settle(self._page)
            await asyncio.sleep(0.5)
            snap = await self.snapshot()
            snap["new_downloads"] = self.downloads[before:]
            return snap

    async def type(self, ref: int, text: str, submit: bool) -> dict:
        async with self._lock:
            loc = await self._locator(ref)
            await loc.fill(text, timeout=10_000)
            if submit:
                await loc.press("Enter")
                await self._settle(self._page)
            return await self.snapshot()

    async def select(self, ref: int, option: str) -> dict:
        async with self._lock:
            loc = await self._locator(ref)
            try:
                await loc.select_option(label=option, timeout=5_000)
            except Exception:
                await loc.select_option(value=option, timeout=5_000)
            await self._settle(self._page)
            return await self.snapshot()

    async def press(self, key: str) -> dict:
        async with self._lock:
            page = await self._ensure()
            await page.keyboard.press(key)
            await self._settle(page)
            return await self.snapshot()

    async def scroll(self, direction: str) -> dict:
        async with self._lock:
            page = await self._ensure()
            js = {"down": "window.scrollBy(0, window.innerHeight*0.85)", "up": "window.scrollBy(0, -window.innerHeight*0.85)",
                  "top": "window.scrollTo(0, 0)", "bottom": "window.scrollTo(0, document.body.scrollHeight)"}[direction]
            await page.evaluate(js)
            await asyncio.sleep(0.4)
            return await self.snapshot()

    async def history(self, action: str) -> dict:
        async with self._lock:
            page = await self._ensure()
            if action == "back":
                await page.go_back(timeout=15_000)
            elif action == "forward":
                await page.go_forward(timeout=15_000)
            else:
                await page.reload(timeout=30_000)
            await self._settle(page)
            return await self.snapshot()

    async def screenshot(self, full_page: bool) -> bytes:
        async with self._lock:
            page = await self._ensure()
            return await page.screenshot(full_page=full_page, type="jpeg", quality=80)

    async def upload(self, ref: int, path: str) -> dict:
        async with self._lock:
            loc = await self._locator(ref)
            el = self.element(ref) or {}
            if el.get("tag") == "input" and el.get("type") == "file":
                await loc.set_input_files(path)
            else:
                async with self._page.expect_file_chooser(timeout=10_000) as fc:
                    await loc.click()
                await (await fc.value).set_files(path)
            await self._settle(self._page)
            return await self.snapshot()

    async def tabs(self) -> list[dict]:
        await self._ensure()
        return [{"index": i, "title": await p.title(), "url": p.url, "active": p is self._page}
                for i, p in enumerate([p for p in self._ctx.pages if not p.is_closed()])]

    async def switch_tab(self, index: int, close: bool = False) -> dict:
        async with self._lock:
            await self._ensure()
            pages = [p for p in self._ctx.pages if not p.is_closed()]
            if not 0 <= index < len(pages):
                raise BrowserError(f"No tab {index}; there are {len(pages)} tabs.")
            if close:
                await pages[index].close()
                pages = [p for p in self._ctx.pages if not p.is_closed()]
                self._page = pages[-1] if pages else await self._ctx.new_page()
            else:
                self._page = pages[index]
                await self._page.bring_to_front()
            return await self.snapshot()

    async def bing_search(self, query: str, max_results: int) -> list[dict]:
        async with self._lock:
            page = await self._ensure()
            await page.goto(f"https://www.bing.com/search?q={quote_plus(query)}", wait_until="domcontentloaded",
                            timeout=30_000)
            await asyncio.sleep(1.0)
            results = await page.evaluate(BING_JS)
            return results[:max_results]


def _is_internal(url: str) -> bool:
    return url.startswith(("edge://", "chrome://", "devtools://", "chrome-extension://"))


def domain(url: str) -> str:
    return urlparse(url).hostname or url


browser = BrowserManager()   # single SALU-wide instance (configured by SaluApp)
