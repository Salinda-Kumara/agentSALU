"""Risk rules for browser actions.

  * Only http(s) URLs; SALU's own local server is off limits.
  * Following links / reading pages / typing into ordinary fields: LOW.
  * Clicking buttons or submitting forms: MEDIUM.
  * Anything that looks like buying, paying, signing in, account changes,
    deleting, sending/posting, or happens on a page with password/payment
    fields: HIGH (always confirmed).
  * Element unknown (stale reference): HIGH.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .permissions import Risk
from .redact import contains_secret

SENSITIVE_RE = re.compile(
    r"\b(buy|purchase|pay|payment|checkout|check out|place (your )?order|order now|complete (order|purchase)|"
    r"add to cart|subscribe|donate|transfer|send money|wire|withdraw|delete|remove|erase|deactivate|"
    r"close (my )?account|cancel (my )?(subscription|order|plan|membership)|sign ?in|log ?in|sign ?up|register|"
    r"create (an )?account|password|passcode|2fa|two-factor|verify|authori[sz]e|allow access|grant|send|post|"
    r"publish|tweet|reply|share|submit|confirm|book now|reserve|upgrade|install|merge|deploy|approve|unsubscribe)\b",
    re.IGNORECASE)
COOKIE_RE = re.compile(r"cookie", re.IGNORECASE)
LOW_ROLES = {"link", "tab", "option", "treeitem"}


class UrlRejected(ValueError):
    pass


def check_url(url: str, salu_port: int | None = None) -> str:
    url = (url or "").strip()
    # "scheme:" (javascript:, data:, file:, ...) but not "host:port" (localhost:3000)
    if not re.match(r"^[a-z][a-z0-9+.\-]*:(?!\d)", url, re.I):
        url = "https://" + url  # bare "example.com" or "localhost:3000"
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise UrlRejected(f"Only http(s) URLs are allowed (got '{u.scheme}:').")
    if not u.hostname:
        raise UrlRejected("URL has no host.")
    if salu_port and u.hostname in ("127.0.0.1", "localhost", "::1") and (u.port or 80) == salu_port:
        raise UrlRejected("SALU's own control interface can't be opened in the automated browser.")
    return url


def _is_search_field(el: dict) -> bool:
    blob = " ".join([el.get("type", ""), el.get("name", ""), el.get("formRole", ""), el.get("formAction", ""),
                     el.get("role", "")]).lower()
    return el.get("type") == "search" or "search" in blob


def element_risk(el: dict | None, flags: dict | None = None) -> tuple[Risk, str]:
    flags = flags or {}
    if not el:
        return Risk.HIGH, "element not found in the latest page snapshot"
    name = el.get("name", "")
    if el.get("type") == "password" or (el.get("autocomplete", "").startswith(("cc-", "current-password", "new-password"))):
        return Risk.HIGH, "password or payment field"
    if COOKIE_RE.search(name) and len(name) < 60:
        return Risk.LOW, "cookie banner"
    if SENSITIVE_RE.search(f"{name} {el.get('formAction', '')}"):
        return Risk.HIGH, f"'{name[:60]}' looks like a sensitive action (purchase/account/sign-in/delete/send)"
    tag, role = el.get("tag", ""), el.get("role", "")
    is_link = (tag == "a" and el.get("href", "").startswith(("http:", "https:"))) or role in LOW_ROLES or tag == "summary"
    if is_link and not el.get("inForm"):
        return Risk.LOW, "follows a link"
    if flags.get("hasPayment"):
        return Risk.HIGH, "page contains payment fields"
    if flags.get("hasPassword") and tag in ("button", "input"):
        return Risk.HIGH, "page contains a sign-in/password form"
    if _is_search_field(el):
        return Risk.LOW, "search form"
    if tag in ("input", "textarea", "select") and el.get("type") not in ("submit", "button", "image", "checkbox", "radio", "file"):
        return Risk.LOW, "focuses an input field"
    return Risk.MEDIUM, f"clicks {tag or role or 'element'} '{name[:60]}'"


def type_risk(el: dict | None, text: str, submit: bool, flags: dict | None = None) -> tuple[Risk, str]:
    if not el:
        return Risk.HIGH, "element not found in the latest page snapshot"
    if el.get("type") == "password" or el.get("autocomplete", "").startswith(("cc-", "current-password", "new-password")):
        return Risk.HIGH, "typing into a password or payment field"
    if contains_secret(text):
        return Risk.HIGH, "text looks like a secret"
    if not submit:
        return Risk.LOW, "types into a field (no submit)"
    if _is_search_field(el):
        return Risk.LOW, "submits a search"
    if (flags or {}).get("hasPassword") or (flags or {}).get("hasPayment"):
        return Risk.HIGH, "submits a form on a sign-in/payment page"
    if SENSITIVE_RE.search(f"{el.get('formAction', '')} {el.get('name', '')}"):
        return Risk.HIGH, "submits a sensitive form"
    return Risk.MEDIUM, "submits a form"
