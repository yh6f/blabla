"""Fast checking engine for gunslol.py.

Two parts:

1. ``SiteProbe`` - checks a profile over plain HTTP (no browser) when the site
   renders its verdict server-side. guns.lol and jugs.lol do; haunt.gg answers
   a WAF page to plain requests, so it falls back to Chrome.
2. ``Pacer`` - a per-site adaptive throttle. It learns from every response:
   a 429 / challenge / block doubles the gap for that site, clean answers slowly
   close it again. This keeps the tool as fast as each site allows without
   hammering it, and ``benchmark`` can measure a polite starting pace on demand.

The benchmark never "floods" a site: it is bounded (default 8 requests), starts
slow, and stops immediately on the first pushback.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

import requests

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

CHALLENGE_MARKERS = ("please wait", "just a moment", "checking your browser",
                     "attention required", "verify you are human", "blocked",
                     "access denied", "enable javascript", "cf-chl")
RATE_MARKERS = ("rate limit", "too many requests", "error 1015")


@dataclass
class SiteState:
    """Live numbers for one site (used by the pacer and the summary)."""

    interval: float
    requests: int = 0
    ok: int = 0
    limited: int = 0
    errors: int = 0
    latency_total: float = 0.0
    limited_at: int = 0
    last_outcome: str = ""

    @property
    def average_latency(self) -> float:
        return self.latency_total / self.requests if self.requests else 0.0


PROFILES = {
    # mode:      base gap, minimum gap, maximum gap, parallel requests
    "steady":   {"base": 0.90, "min": 0.45, "max": 12.0, "workers": 1},
    "fast":     {"base": 0.35, "min": 0.12, "max": 10.0, "workers": 3},
    "turbo":    {"base": 0.12, "min": 0.04, "max": 8.0, "workers": 6},
    "adaptive": {"base": 0.30, "min": 0.06, "max": 12.0, "workers": 4},
}

MODE_LABELS = {
    "steady": "Steady - one request at a time, safest",
    "fast": "Fast - short gaps, 3 parallel sites",
    "turbo": "Turbo - minimum gaps, 6 parallel requests",
    "adaptive": "Adaptive - learns each site's limit (recommended)",
}


class Pacer:
    """Thread-safe per-site adaptive throttle."""

    def __init__(self, mode="adaptive", minimum_override=None, max_per_site=None):
        profile = PROFILES.get(mode, PROFILES["adaptive"])
        self.mode = mode if mode in PROFILES else "adaptive"
        self.base = profile["base"]
        self.minimum = profile["min"]
        self.maximum = profile["max"]
        self.workers = min(profile["workers"], max_per_site or profile["workers"])
        if minimum_override and minimum_override > 0:
            self.minimum = max(self.minimum, float(minimum_override))
        self._states = {}
        self._next_at = {}
        self._locks = {}
        self._guard = threading.Lock()

    # ----------------------------------------------------------------- state
    def state(self, site):
        with self._guard:
            if site not in self._states:
                self._states[site] = SiteState(interval=self.base)
                self._next_at[site] = 0.0
                self._locks[site] = threading.Lock()
            return self._states[site]

    def states(self):
        with self._guard:
            return dict(self._states)

    def interval(self, site):
        return self.state(site).interval

    # ------------------------------------------------------------- throttling
    def acquire(self, site):
        """Blocks until this site's next allowed request slot, then reserves it."""
        state = self.state(site)
        lock = self._locks[site]
        with lock:
            wait = self._next_at[site] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_at[site] = time.monotonic() + state.interval

    def record(self, site, outcome, latency=0.0):
        """Feeds a real outcome back into the pacing model.

        'unclaimed' / 'claimed'     -> clean answer, gently reduce the gap
        'ratelimited' / 'challenge' -> back off (x2) and remember where it broke
        anything else               -> mild backoff
        """
        state = self.state(site)
        with self._guard:
            state.requests += 1
            state.latency_total += max(latency, 0.0)
            state.last_outcome = outcome
            if outcome in ("claimed", "unclaimed"):
                state.ok += 1
                if state.ok % 4 == 0:
                    state.interval = max(self.minimum, state.interval * 0.92)
            elif outcome in ("ratelimited", "challenge"):
                state.limited += 1
                state.limited_at = state.requests
                state.interval = min(self.maximum, max(state.interval * 2.0, self.base * 2))
            else:
                state.errors += 1
                state.interval = min(self.maximum, state.interval * 1.25)
            self._next_at[site] = time.monotonic() + state.interval

    def backoff_hint(self, site):
        return min(self.state(site).interval, self.maximum)

    def estimated_per_minute(self):
        return {site: (60.0 / state.interval if state.interval else 0.0)
                for site, state in self.states().items()}


@dataclass
class ProbeResult:
    verdict: object = None        # 'unclaimed' | 'claimed' | None (needs Chrome)
    status: int = 0
    latency: float = 0.0
    reason: str = ""


def _fold(text):
    return (text or "").casefold().replace("\u0131", "i").replace("\u0307", "")


def _extract_title(html):
    start = html.lower().find("<title")
    if start < 0:
        return ""
    start = html.find(">", start)
    end = html.lower().find("</title>", start)
    if start < 0 or end < 0:
        return ""
    return html[start + 1:end].strip()


class SiteProbe:
    """Plain-HTTP probe for a site that renders its verdict server-side.

    Uses a requests session (cookies follow guns.lol's clearance redirect) and
    never raises: unclear pages return ``verdict=None`` so Chrome handles them.
    """

    def __init__(self, site, proxies=None, timeout=12):
        self.site = site
        self.proxies = list(proxies or [])
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })
        self.requests_made = 0

    def _proxy(self):
        if not self.proxies:
            return {}
        proxy = random.choice(self.proxies)
        auth = ""
        if proxy.get("username"):
            from urllib.parse import quote
            auth = "{}:{}@".format(quote(proxy["username"], safe=""),
                                   quote(proxy["password"], safe=""))
        url = "http://{}{}:{}".format(auth, proxy["host"], proxy["port"])
        return {"http": url, "https": url}

    def fetch(self, path=""):
        """GETs a page. Returns (response, latency) or raises requests.RequestException."""
        url = self.site.profile_url(path) if path else self.site.url.split("{username}")[0].rstrip("/")
        started = time.monotonic()
        response = self.session.get(url, timeout=self.timeout, allow_redirects=True,
                                    proxies=self._proxy() or None)
        self.requests_made += 1
        return response, time.monotonic() - started

    def check(self, username):
        """Classifies one username over HTTP. Never raises."""
        started = time.monotonic()
        try:
            response, _ = self.fetch(username)
        except requests.RequestException as exc:
            return ProbeResult(None, 0, time.monotonic() - started, type(exc).__name__)
        latency = time.monotonic() - started
        status = response.status_code
        html = response.text[:60000]
        title = _extract_title(html)
        folded_title = _fold(title)
        folded_html = _fold(html)

        if any(marker in folded_title for marker in RATE_MARKERS):
            return ProbeResult("ratelimited", status, latency, "rate limit title")
        if any(marker in folded_title for marker in CHALLENGE_MARKERS):
            return ProbeResult("challenge", status, latency, "challenge title")
        if status in self.site.status_not_found:
            return ProbeResult("unclaimed", status, latency, "HTTP {}".format(status))
        if status in (429, 401, 403, 503):
            return ProbeResult("challenge", status, latency, "HTTP {}".format(status))
        # Claimed-profile marker first: guns.lol profiles embed a "User Not found" widget.
        if self.site.claimed and any(marker in folded_title for marker in self.site.claimed):
            return ProbeResult("claimed", status, latency, "profile title")
        if self.site.available and any(marker in folded_html for marker in self.site.available):
            return ProbeResult("unclaimed", status, latency, "claim marker")
        if self.site.not_found and any(marker in folded_title for marker in self.site.not_found):
            return ProbeResult("unclaimed", status, latency, "not-found title")
        if self.site.claimed and folded_title.startswith("@"):
            return ProbeResult("claimed", status, latency, "profile title")
        if self.site.not_found and any(
                "<h1>{}</h1>".format(marker).replace("&", "&amp;") in folded_html
                for marker in self.site.not_found if len(marker) > 6):
            return ProbeResult("unclaimed", status, latency, "not-found heading")
        if status == 200 and getattr(self.site, "fast_http_claimed", False):
            return ProbeResult("claimed", status, latency, "HTTP 200")
        return ProbeResult(None, status, latency, "needs browser")


class FastEngine:
    """HTTP probes + pacer for all selected sites."""

    def __init__(self, sites, proxies=None, mode="adaptive", minimum_interval=None):
        self.pacer = Pacer(mode, minimum_interval)
        self.probes = {}
        self.skipped = []
        for site in sites:
            if getattr(site, "fast_http", False):
                self.probes[site.name] = SiteProbe(site, proxies)
            else:
                self.skipped.append(site.name)

    @property
    def enabled(self):
        return bool(self.probes)

    def sites_for_http(self):
        return list(self.probes)

    def check(self, site, username):
        """Checks one site over HTTP, paced. Returns a ProbeResult."""
        self.pacer.acquire(site.name)
        probe = self.probes.get(site.name)
        if probe is None:
            return ProbeResult(None, 0, 0.0, "no HTTP probe")
        result = probe.check(username)
        if result.verdict in ("unclaimed", "claimed", "ratelimited", "challenge"):
            self.pacer.record(site.name, result.verdict, result.latency)
        else:
            self.pacer.record(site.name, "error" if result.status == 0 else "unclear", result.latency)
        return result


# ------------------------------------------------------------------ benchmark

@dataclass
class BenchmarkReport:
    site: str
    requests: int = 0
    clean: int = 0
    limited: int = 0
    safe_interval: float = 0.0
    fastest_clean_interval: float = 0.0
    stopped_by: str = ""
    samples: list = field(default_factory=list)

    @property
    def per_minute(self):
        return 60.0 / self.safe_interval if self.safe_interval else 0.0

    def lines(self):
        rows = [
            "{}: {} request(s), {} clean, {} pushed back".format(
                self.site, self.requests, self.clean, self.limited),
            "estimated safe gap: {:.2f}s (~{:.0f} names/min)".format(
                self.safe_interval, self.per_minute),
        ]
        if self.stopped_by:
            rows.append("stopped: {}".format(self.stopped_by))
        return rows


def benchmark(pacer, site, max_requests=8, start_interval=0.8, floor=0.12,
              step=0.75, callback=None):
    """Measures a polite starting pace for a site.

    Starts at ``start_interval`` and shortens the gap by ``step`` after each
    clean answer, stopping immediately on the first rate-limit / block / error.
    The result keeps a 1.6x safety margin. Bounded by ``max_requests``.
    """
    report = BenchmarkReport(site=site.name)
    gap = max(start_interval, floor)
    probe = SiteProbe(site)
    if callback:
        callback("Starting a bounded benchmark of {} (max {} requests)...".format(site.name, max_requests))
    time.sleep(0.4)  # be a polite neighbour before the first request
    for index in range(max_requests):
        if callback:
            callback("  request {}/{} at {:.2f}s gap".format(index + 1, max_requests, gap))
        pacer.acquire(site.name)
        result = probe.check("")
        report.requests += 1
        report.samples.append(round(result.latency, 3))
        if result.verdict in ("claimed", "unclaimed") or result.status in (200, 404, 410):
            report.clean += 1
            report.fastest_clean_interval = gap
            pacer.record(site.name, "claimed", result.latency)
        else:
            report.limited += 1
            report.stopped_by = result.reason or "pushback"
            pacer.record(site.name, "ratelimited" if result.status in (429, 403) else "error",
                         result.latency)
            break
        gap = max(floor, gap * step)
        time.sleep(gap)
    report.safe_interval = max(floor, (report.fastest_clean_interval or start_interval) * 1.6)
    if report.clean == 0:
        report.safe_interval = 0.0
        report.stopped_by = report.stopped_by or "no clean answer"
    return report


def tune_advice(report):
    """Human-readable recommendation from a benchmark report."""
    if not report.clean:
        return "no safe pace found - keep this site on Steady and use proxies"
    if report.safe_interval <= 0.2:
        return "very responsive - Fast or Adaptive mode is safe"
    if report.safe_interval <= 0.6:
        return "moderate - Adaptive mode will settle around {:.2f}s".format(report.safe_interval)
    return "strict - keep Steady (about {:.1f}s between requests)".format(report.safe_interval)
