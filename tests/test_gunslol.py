"""Offline test-suite for gunslol.py - no Chrome and no internet needed.

Run from the project folder:
    python -m unittest discover -s tests -v
"""

import base64
import contextlib
import http.server
import io
import json
import os
import select
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generator as listgen  # noqa: E402
import speed as fastpath  # noqa: E402
import gunslol  # noqa: E402
from selenium.common.exceptions import TimeoutException, WebDriverException  # noqa: E402

_ORIGINAL_VALID_WEBHOOK = gunslol.is_valid_webhook_url


def _is_valid(url):
    """Accepts the fake Discord on localhost in addition to real Discord webhooks."""
    return str(url).startswith("http://127.0.0.1") or _ORIGINAL_VALID_WEBHOOK(str(url))


# --------------------------------------------------------------------------- helpers

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pipe(a, b):
    sockets = [a, b]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 5)
            if not readable:
                return
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                (b if sock is a else a).sendall(data)
    except OSError:
        return


class EchoHandler(http.server.BaseHTTPRequestHandler):
    """Origin server: echoes the request path + headers as JSON."""

    def do_GET(self):
        body = json.dumps({"path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeUpstreamProxy:
    """Authenticating HTTP proxy (CONNECT + absolute-form requests) used as an upstream."""

    def __init__(self, name, credentials=("user", "pass")):
        self.name = name
        token = base64.b64encode("{}:{}".format(*credentials).encode()).decode()
        self.expected = "Basic " + token
        self.seen = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            head, rest = gunslol._read_http_head(conn)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split()
            headers = {}
            for line in lines[1:]:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
            auth_ok = headers.get("proxy-authorization") == self.expected
            self.seen.append((method, auth_ok))
            if not auth_ok:
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                             b"Proxy-Authenticate: Basic realm=x\r\nContent-Length: 0\r\n\r\n")
                return
            if method == "CONNECT":
                host, port = target.rsplit(":", 1)
                upstream = socket.create_connection((host, int(port)))
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                parsed = urllib.parse.urlsplit(target)
                upstream = socket.create_connection((parsed.hostname, parsed.port or 80))
                forwarded = ["{} {} HTTP/1.1".format(method, parsed.path or "/")]
                forwarded += [line for line in lines[1:] if not line.lower().startswith("proxy-")]
                forwarded.append("X-Upstream: " + self.name)
                upstream.sendall(("\r\n".join(forwarded) + "\r\n\r\n").encode("latin-1") + rest)
            _pipe(conn, upstream)
            upstream.close()
        except Exception:
            pass
        finally:
            conn.close()

    def close(self):
        self.server.close()


def _receive_all(sock):
    chunks = []
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _plain_request(proxy_port, origin_port, path="/plain"):
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=10) as sock:
        sock.sendall("GET http://127.0.0.1:{0}{1} HTTP/1.1\r\nHost: 127.0.0.1:{0}\r\n"
                     "Proxy-Connection: keep-alive\r\n\r\n".format(origin_port, path).encode())
        return _receive_all(sock)


def _json_body(response):
    return json.loads(response.split(b"\r\n\r\n", 1)[1].decode())


# --------------------------------------------------------------------------- usernames

class UsernameTests(unittest.TestCase):
    def test_random_letters_length_and_charset(self):
        for n in range(1, 9):
            for _ in range(200):
                name = gunslol.random_letters(n)
                self.assertEqual(len(name), n)
                self.assertTrue(set(name) <= set(gunslol.ALL_CHARS))

    def test_random_letters_premium_filter(self):
        for n in (1, 2, 3, 6):
            for _ in range(500):
                self.assertFalse(gunslol.is_premium_alias(gunslol.random_letters(n, True)))

    def test_count_combinations(self):
        self.assertEqual(gunslol.count_combinations(1), 39)
        self.assertEqual(gunslol.count_combinations(1, True), 36)
        self.assertEqual(gunslol.count_combinations(2, True), 36 * 36)
        self.assertEqual(gunslol.count_combinations(3, True), 36 * 36 * 39)

    def test_is_premium_alias(self):
        for name in (".abc", "abc_", "-a", "_"):
            self.assertTrue(gunslol.is_premium_alias(name), name)
        for name in ("a.b", "abc", "a-b_c", ""):
            self.assertFalse(gunslol.is_premium_alias(name), name)

    def test_normalize_username(self):
        self.assertEqual(gunslol.normalize_username("https://guns.lol/Efe/"), "efe")
        self.assertEqual(gunslol.normalize_username("http://www.guns.lol/a.b"), "a.b")
        self.assertEqual(gunslol.normalize_username("guns.lol/abc"), "abc")
        self.assertEqual(gunslol.normalize_username("  @Name "), "name")

    def test_random_source_is_unique_and_exhausts(self):
        source = gunslol.UsernameSource(1, None, True)
        names = list(source)
        self.assertEqual(len(names), 36)
        self.assertEqual(len(set(names)), 36)
        self.assertTrue(source.exhausted)

    def test_custom_source_retry_pass(self):
        source = gunslol.UsernameSource(None, ["a", "b"])
        self.assertEqual(next(source), "a")
        self.assertTrue(source.defer("a"))
        self.assertFalse(source.defer("a"))
        self.assertEqual(next(source), "b")
        self.assertFalse(source.in_retry_pass)
        self.assertEqual(next(source), "a")
        self.assertTrue(source.in_retry_pass)
        with self.assertRaises(StopIteration):
            next(source)


# --------------------------------------------------------------------------- files

class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "file.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data):
        with open(self.path, "wb") as handle:
            handle.write(data)

    def test_customlist_parsing(self):
        content = ("// comment\n# also a comment\n\nEfe\nefe\nhttps://guns.lol/Abc/\n"
                   "bad name\n@x_y\nvalid.name\n")
        self._write(b"\xef\xbb\xbf" + content.encode("utf-8"))  # Notepad BOM
        names, invalid, duplicates = gunslol.load_customlist(self.path)
        self.assertEqual(names, ["efe", "abc", "x_y", "valid.name"])
        self.assertEqual(invalid, ["bad name"])
        self.assertEqual(duplicates, 1)

    def test_customlist_utf16(self):
        self._write("efe\nkrbs\n".encode("utf-16"))
        self.assertEqual(gunslol.load_customlist(self.path)[0], ["efe", "krbs"])

    def test_customlist_missing(self):
        with self.assertRaises(FileNotFoundError):
            gunslol.load_customlist(os.path.join(self.tmp.name, "nope.txt"))

    def test_hit_writer(self):
        self._write(b"old")  # no trailing newline
        writer = gunslol.HitWriter(self.path)
        self.assertEqual(writer.add("new"), "saved")
        self.assertEqual(writer.add("OLD"), "duplicate")
        self.assertEqual(writer.add("new"), "duplicate")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old\nnew\n")
        self.assertEqual(writer.written, 1)


# --------------------------------------------------------------------------- proxies

class ProxyParsingTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(gunslol.parse_proxy("1.2.3.4:8080:user:pa:ss"),
                         {"host": "1.2.3.4", "port": 8080, "username": "user", "password": "pa:ss"})
        self.assertEqual(gunslol.parse_proxy("user:pass@host:80")["host"], "host")
        self.assertEqual(gunslol.parse_proxy("http://user:pass@host:81/")["port"], 81)
        self.assertEqual(gunslol.parse_proxy("host:3128")["username"], "")

    def test_errors(self):
        for bad in ("host:port", "host:70000", "socks5://h:1", "h:1:user", "h:1:user:", ":80"):
            with self.assertRaises(ValueError, msg=bad):
                gunslol.parse_proxy(bad)

    def test_load_proxies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "proxies.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# comment\n// comment\n1.1.1.1:80:u:p\n1.1.1.1:80:u:p\n2.2.2.2:81\n")
            self.assertEqual(len(gunslol.load_proxies(path)), 2)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("broken\n")
            with self.assertRaisesRegex(ValueError, "line 6"):
                gunslol.load_proxies(path)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# only comments\n")
            with self.assertRaisesRegex(ValueError, "does not contain any usable proxies"):
                gunslol.load_proxies(path)

    def test_requests_config_escapes_credentials(self):
        config = gunslol.get_requests_proxy_config(
            {"host": "h", "port": 1, "username": "us@r", "password": "p:ss/w"})
        self.assertEqual(config["https"], "http://us%40r:p%3Ass%2Fw@h:1")


class ProxyRotatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        cls.origin_port = cls.origin.server_address[1]
        threading.Thread(target=cls.origin.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()
        cls.origin.server_close()

    def setUp(self):
        self.upstreams = []
        self.rotators = []

    def tearDown(self):
        for rotator in self.rotators:
            rotator.stop()
        for upstream in self.upstreams:
            upstream.close()

    def _upstream(self, name, credentials=("user", "pass")):
        upstream = FakeUpstreamProxy(name, credentials)
        self.upstreams.append(upstream)
        return {"host": "127.0.0.1", "port": upstream.port, "username": "user", "password": "pass"}

    def _rotator(self, proxies):
        rotator = gunslol.ProxyRotator(proxies).start()
        self.rotators.append(rotator)
        return rotator

    def test_plain_http_gets_auth_injected(self):
        rotator = self._rotator([self._upstream("A")])
        response = _plain_request(rotator.port, self.origin_port)
        self.assertTrue(response.startswith(b"HTTP/1.0 200") or response.startswith(b"HTTP/1.1 200"))
        headers = _json_body(response)["headers"]
        self.assertEqual(headers["x-upstream"], "A")
        self.assertNotIn("proxy-authorization", headers)  # never leaked to the website
        self.assertEqual(self.upstreams[0].seen, [("GET", True)])

    def test_connect_tunnel(self):
        rotator = self._rotator([self._upstream("A")])
        with socket.create_connection(("127.0.0.1", rotator.port), timeout=10) as sock:
            sock.sendall("CONNECT 127.0.0.1:{0} HTTP/1.1\r\nHost: 127.0.0.1:{0}\r\n\r\n"
                         .format(self.origin_port).encode())
            head, rest = gunslol._read_http_head(sock)
            self.assertIn(b" 200 ", head + b" ")
            sock.sendall(b"GET /tunnel HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            response = rest + _receive_all(sock)
        self.assertEqual(_json_body(response)["path"], "/tunnel")
        self.assertEqual(self.upstreams[0].seen, [("CONNECT", True)])

    def test_rotates_between_usernames(self):
        rotator = self._rotator([self._upstream("A"), self._upstream("B")])
        used = []
        for _ in range(3):
            used.append(_json_body(_plain_request(rotator.port, self.origin_port))["headers"]["x-upstream"])
            rotator.rotate()
        self.assertEqual(used, ["A", "B", "A"])

    def test_failover_and_benching(self):
        dead = {"host": "127.0.0.1", "port": _free_port(), "username": "user", "password": "pass"}
        wrong_auth = self._upstream("C", credentials=("other", "creds"))
        good = self._upstream("A")
        rotator = self._rotator([dead, wrong_auth, good])
        response = _plain_request(rotator.port, self.origin_port)
        self.assertEqual(_json_body(response)["headers"]["x-upstream"], "A")
        self.assertEqual(rotator.healthy_count(), 2)  # 407 proxy benched right away
        response = _plain_request(rotator.port, self.origin_port)
        self.assertEqual(_json_body(response)["headers"]["x-upstream"], "A")
        self.assertEqual(rotator.healthy_count(), 1)  # dead proxy benched after 2 failures

    def test_all_proxies_down_returns_502(self):
        dead = {"host": "127.0.0.1", "port": _free_port(), "username": "", "password": ""}
        rotator = self._rotator([dead])
        self.assertTrue(_plain_request(rotator.port, self.origin_port).startswith(b"HTTP/1.1 502"))

    def test_rotate_drops_open_tunnels(self):
        rotator = self._rotator([self._upstream("A"), self._upstream("B")])
        sock = socket.create_connection(("127.0.0.1", rotator.port), timeout=5)
        sock.sendall("CONNECT 127.0.0.1:{0} HTTP/1.1\r\nHost: x\r\n\r\n".format(self.origin_port).encode())
        gunslol._read_http_head(sock)
        rotator.rotate()
        self.assertEqual(sock.recv(10), b"")  # tunnel closed -> Chrome reconnects via proxy B
        sock.close()


# --------------------------------------------------------------------------- webhooks

class FakeDiscord(http.server.BaseHTTPRequestHandler):
    responses = []
    received = []

    def _reply(self):
        status, headers, body = FakeDiscord.responses.pop(0) if FakeDiscord.responses else (204, {}, b"")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        FakeDiscord.received.append(json.loads(self.rfile.read(length)))
        self._reply()

    def do_GET(self):
        self._reply()

    def log_message(self, *args):
        pass


class WebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeDiscord)
        cls.url = "http://127.0.0.1:{}/api/webhooks/1/token".format(cls.server.server_address[1])
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeDiscord.responses = []
        FakeDiscord.received = []
        # The fake Discord runs on localhost; the production validator only allows discord.com.
        patch = mock.patch.object(gunslol, "is_valid_webhook_url", lambda url: _is_valid(url))
        patch.start()
        self.addCleanup(patch.stop)

    def test_url_validation(self):
        valid = ["https://discord.com/api/webhooks/123/abc-DEF_1",
                 "https://discordapp.com/api/webhooks/123/abc",
                 "https://ptb.discord.com/api/v10/webhooks/123/abc/",
                 "https://canary.discord.com/api/webhooks/123/abc?thread_id=5"]
        invalid = ["https://discord.com/api/webhooks/", "http://discord.com/api/webhooks/1/a",
                   "https://evil.com/api/webhooks/1/a", "https://discord.com.evil.io/api/webhooks/1/a"]
        for url in valid:
            self.assertTrue(gunslol.is_valid_webhook_url(url), url)
        for url in invalid:
            self.assertFalse(gunslol.is_valid_webhook_url(url), url)

    def test_payload_keeps_v1_format(self):
        embed = gunslol.build_webhook_payload("efe")["embeds"][0]
        self.assertEqual(embed["title"], "Available: efe (https://guns.lol/efe)")
        self.assertEqual(embed["description"], "github.com/efekrbas")
        self.assertEqual(embed["color"], 0x9B59B6)
        self.assertEqual(embed["url"], "https://guns.lol/efe")

    def test_notifier_retries_after_429(self):
        FakeDiscord.responses = [(429, {"Retry-After": "0.1"}, b'{"retry_after": 0.1}'), (204, {}, b"")]
        notifier = gunslol.WebhookNotifier(self.url)
        notifier.notify("abc")
        notifier.close()
        self.assertEqual((notifier.sent, notifier.failed), (1, 0))
        self.assertEqual(len(FakeDiscord.received), 2)

    def test_notifier_routes_per_site(self):
        # One channel for jugs.lol, one for everything else.
        FakeDiscord.responses = [(204, {}, b"")] * 4
        jugs_only = {"label": "jugs", "url": self.url, "route": ("jugs.lol",)}
        catch_all = {"label": "all", "url": self.url.replace("/webhooks/1/", "/webhooks/2/"),
                     "route": ("*",)}
        notifier = gunslol.WebhookNotifier([jugs_only, catch_all])
        jugs, haunt = gunslol.BUILTIN_SITES[2], gunslol.BUILTIN_SITES[1]
        notifier.notify("efe", jugs)      # both channels
        notifier.notify("abc", haunt)     # only the catch-all channel
        notifier.close()
        self.assertEqual((notifier.sent, notifier.failed), (3, 0))
        self.assertEqual(len(FakeDiscord.received), 3)
        self.assertEqual(notifier.delivered, {"jugs": 1, "all": 2})
        self.assertEqual(notifier.routes_for("jugs.lol"), ["jugs", "all"])
        self.assertEqual(notifier.routes_for("haunt.gg"), ["all"])
        titles = [payload["embeds"][0]["title"] for payload in FakeDiscord.received]
        self.assertIn("Available: abc (https://haunt.gg/abc)", titles)

    def test_notifier_skips_when_no_route_matches(self):
        FakeDiscord.responses = [(204, {}, b"")]
        notifier = gunslol.WebhookNotifier([{"label": "jugs", "url": self.url,
                                             "route": ("jugs.lol",)}])
        self.assertEqual(notifier.routes_for("guns.lol"), [])
        notifier.notify("abc", gunslol.BUILTIN_SITES[1])   # haunt.gg -> no match
        notifier.close()
        self.assertEqual((notifier.sent, notifier.failed, len(FakeDiscord.received)), (0, 0, 0))
        self.assertEqual(notifier.skipped, {"haunt.gg": 1})

    def test_send_gives_up_on_404(self):
        FakeDiscord.responses = [(404, {}, b"{}")]
        self.assertEqual(gunslol.send_discord_webhook(self.url, "abc"), "invalid")
        self.assertEqual(len(FakeDiscord.received), 1)

    def test_notifier_fans_out_with_site(self):
        FakeDiscord.responses = [(204, {}, b""), (204, {}, b"")]
        second = self.url.replace("/webhooks/1/", "/webhooks/2/")
        notifier = gunslol.WebhookNotifier([{"label": "one", "url": self.url},
                                            {"label": "two", "url": second}])
        notifier.notify("efe", gunslol.BUILTIN_SITES[0])
        notifier.close()
        self.assertEqual((notifier.sent, notifier.failed), (2, 0))
        self.assertEqual(len(FakeDiscord.received), 2)
        site_field = FakeDiscord.received[0]["embeds"][0]["fields"][0]
        self.assertEqual(site_field, {"name": "Site", "value": "guns.lol", "inline": True})

    def test_verify_webhook(self):
        FakeDiscord.responses = [(200, {"Content-Type": "application/json"}, b'{"name": "hits"}'),
                                 (404, {}, b"{}")]
        self.assertEqual(gunslol.verify_webhook(self.url), ("ok", "hits"))
        self.assertEqual(gunslol.verify_webhook(self.url), ("invalid", "HTTP 404"))
        status, _ = gunslol.verify_webhook("http://127.0.0.1:{}/x".format(_free_port()))
        self.assertEqual(status, "unknown")


# --------------------------------------------------------------------------- detection

NOT_FOUND = {"title": "guns.lol", "h1": ["Username not found"], "body": "Username not found"}
CLAIMED = {"title": "efe | guns.lol", "h1": ["efe"], "body": "efe - click to enter"}
CHALLENGE = {"title": "Please wait a moment...", "h1": ["guns.lol"], "body": "guns.lol"}
RATE_LIMIT = {"title": "429 Too Many Requests", "h1": [], "body": ""}


class ClassifyTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(gunslol.classify_page("", [], ""), "loading")
        self.assertEqual(gunslol.classify_page("Please wait a moment...", ["guns.lol"]), "challenge")
        self.assertEqual(gunslol.classify_page("Just a moment...", []), "challenge")
        self.assertEqual(gunslol.classify_page("429 Too Many Requests", []), "ratelimited")
        self.assertEqual(gunslol.classify_page("Access denied | guns.lol used Cloudflare", []), "challenge")
        self.assertEqual(gunslol.classify_page("guns.lol", ["Username not found"]), "unclaimed")
        self.assertEqual(gunslol.classify_page("guns.lol", ["KULLANICI ADI BULUNAMADI"]), "unclaimed")
        self.assertEqual(gunslol.classify_page("guns.lol", ["Kullan\u0131c\u0131 ad\u0131 bulunamad\u0131"]),
                         "unclaimed")
        guns = gunslol.BUILTIN_SITES[0]
        self.assertEqual(gunslol.classify_page(
            "guns.lol: Everything you want, right here.", [],
            "Username not found | Claim this username by clicking the button below!",
            status=200, site=guns), "unclaimed")
        # Real claimed guns.lol page (verified live): its widget renders the h1 "User Not found".
        self.assertEqual(gunslol.classify_page(
            "@Efe | guns.lol", ["Efe", "User Not found"],
            "click to enter... Efe | User Not found | Please connect your Discord account",
            status=200, site=guns), "claimed")
        self.assertEqual(gunslol.classify_page("efe | guns.lol", ["efe"], "username not found"), "claimed")

    def test_http_status_and_widget_safety(self):
        haunt = [site for site in gunslol.BUILTIN_SITES if site.name == "haunt.gg"][0]
        jugs = [site for site in gunslol.BUILTIN_SITES if site.name == "jugs.lol"][0]
        # jugs.lol: missing -> 404 with a "Not found" title; existing -> 200
        self.assertEqual(gunslol.classify_page("jugs.lol — Customizable Bio Link", [], "", status=404,
                                               site=jugs), "unclaimed")
        self.assertEqual(gunslol.classify_page("Not found | jugs.lol", [], "", status=200, site=jugs),
                         "unclaimed")
        self.assertEqual(gunslol.classify_page("jugs.lol — Customizable Bio Link", ["someone"], "profile",
                                               status=200, site=jugs), "claimed")
        # haunt.gg profile pages embed a Discord widget that literally says "Not Found"
        self.assertEqual(gunslol.classify_page("haunt.gg", ["gothless"], "Unavailable\nNot Found\n0 members",
                                               status=200, site=haunt), "claimed")
        self.assertEqual(gunslol.classify_page("haunt.gg | Blocked", ["haunt"], "", status=403, site=haunt),
                         "challenge")
        self.assertEqual(gunslol.classify_page("haunt.gg", ["whatever"], "", status=500, site=haunt),
                         "unknown")


class SiteTests(unittest.TestCase):
    def test_builtin_registry(self):
        sites, problems = gunslol.load_sites(os.path.join(tempfile.gettempdir(), "does-not-exist-xyz.json"))
        self.assertEqual([site.name for site in sites], ["guns.lol", "haunt.gg", "jugs.lol"])
        self.assertEqual(problems, [])
        self.assertTrue(sites[0].premium)
        self.assertFalse(sites[1].premium)
        self.assertEqual(sites[2].profile_url("A.B"), "https://jugs.lol/A.B")

    def test_custom_entries_overrides_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sites.json")
            payload = {"sites": [
                {"name": "bio.site", "url": "https://bio.site/{username}",
                 "not_found": ["page not found"]},
                {"name": "jugs.lol", "url": "https://jugs.lol/{username}", "not_found": ["custom 404"]},
                {"name": "broken", "url": "https://broken.example/no-placeholder"},
                {"name": "off.example", "url": "https://off.example/{username}", "enabled": False},
            ]}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            sites, problems = gunslol.load_sites(path)
        names = [site.name for site in sites]
        self.assertIn("bio.site", names)
        self.assertNotIn("off.example", names)
        self.assertEqual([site for site in sites if site.name == "jugs.lol"][0].not_found,
                         ("custom 404",))
        self.assertTrue(any("broken" in problem for problem in problems), problems)
        custom = [site for site in sites if site.name == "bio.site"][0]
        self.assertEqual(custom.profile_url("x y"), "https://bio.site/x%20y")


class SavedWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "webhooks.txt")
        self.url = "https://discord.com/api/webhooks/123456/tokenA"
        self.url2 = "https://discord.com/api/webhooks/987654/tokenB"

    def test_round_trip(self):
        self.assertEqual(gunslol.load_saved_webhooks(self.path), ([], 0))
        self.assertEqual(gunslol.save_webhook(self.url, "private", self.path), "saved")
        self.assertEqual(gunslol.save_webhook(self.url, "duplicate", self.path), "exists")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("bad line\n{}\n".format(self.url2))
        entries, invalid = gunslol.load_saved_webhooks(self.path)
        self.assertEqual(invalid, 1)
        self.assertEqual([entry["label"] for entry in entries], ["private", "Webhook 7654"])
        # A webhook without a route serves every site.
        self.assertEqual(entries[0]["route"], ("*",))
        masked = gunslol.webhook_mask(entries[0]["url"])
        self.assertIn("discord.com", masked)
        self.assertNotIn("tokenA", masked)
        self.assertEqual(gunslol.remove_saved_webhooks([entries[0]["url"]], self.path), 1)
        self.assertEqual([entry["url"] for entry in gunslol.load_saved_webhooks(self.path)[0]],
                         [self.url2])

    def test_site_routing(self):
        self.assertEqual(gunslol.save_webhook(self.url, "jugs only", self.path, route="jugs.lol"), "saved")
        self.assertEqual(gunslol.save_webhook(self.url2, "everywhere", self.path, route="*"), "saved")
        saved, _ = gunslol.load_saved_webhooks(self.path)
        self.assertEqual(saved[0]["route"], ("jugs.lol",))
        self.assertTrue(gunslol.webhook_serves(saved[0], "jugs.lol"))
        self.assertFalse(gunslol.webhook_serves(saved[0], "guns.lol"))
        self.assertEqual([entry["label"] for entry in gunslol.matching_webhooks(saved, "jugs.lol")],
                         ["jugs only", "everywhere"])
        self.assertEqual([entry["label"] for entry in gunslol.matching_webhooks(saved, "haunt.gg")],
                         ["everywhere"])
        # Rewriting keeps the third field, and re-routing updates it.
        self.assertEqual(gunslol.set_webhook_route(self.url, "guns.lol,haunt.gg", self.path), True)
        saved, _ = gunslol.load_saved_webhooks(self.path)
        self.assertEqual(saved[0]["route"], ("guns.lol", "haunt.gg"))
        self.assertEqual(gunslol.set_webhook_route("https://discord.com/api/webhooks/9/x", "jugs.lol",
                                                   self.path), False)

    def test_indices_parser(self):
        self.assertEqual(gunslol._parse_indices("1,3-4", 5), [0, 2, 3])
        self.assertEqual(gunslol._parse_indices("A", 3), [0, 1, 2])
        self.assertEqual(gunslol._parse_indices("N", 3), [])
        self.assertEqual(gunslol._parse_indices("", 3, empty_means_all=True), [0, 1, 2])
        with self.assertRaises(ValueError):
            gunslol._parse_indices("9", 3)


class FakeDriver:
    """Each get() loads the next page: a list of snapshots returned one by one while polling."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.current = [{"title": "", "h1": [], "body": ""}]
        self.gets = 0

    def get(self, url):
        self.gets += 1
        page = self.pages.pop(0) if self.pages else [CLAIMED]
        if isinstance(page, Exception):
            raise page
        if isinstance(page, tuple):  # ("timeout", snapshots)
            self.current = list(page[1])
            raise TimeoutException("page load timeout")
        self.current = list(page)

    def execute_script(self, script):
        if "window.stop" in script:
            return None
        return self.current.pop(0) if len(self.current) > 1 else self.current[0]


class FakeBrowser:
    def __init__(self, driver, dead=False):
        self.driver = driver
        self.dead = dead
        self.restarts = 0

    def is_dead(self, exc=None):
        return self.dead

    def restart(self):
        self.restarts += 1
        self.dead = False

    def mark_healthy(self):
        pass


class CheckUsernameTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(gunslol, "CHALLENGE_WAIT", 0.2),
                   mock.patch.object(gunslol, "POLL_INTERVAL", 0.01),
                   mock.patch.object(gunslol, "_sleep", lambda seconds: time.sleep(min(seconds, 0.01)))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def check(self, pages, **browser_kwargs):
        driver = FakeDriver(pages)
        browser = FakeBrowser(driver, **browser_kwargs)
        return gunslol.check_username(browser, "efe"), driver, browser

    def test_unclaimed_after_challenge(self):
        result, driver, _ = self.check([[CHALLENGE, CHALLENGE, NOT_FOUND]])
        self.assertEqual((result, driver.gets), ("unclaimed", 1))

    def test_claimed(self):
        self.assertEqual(self.check([[CLAIMED]])[0], "claimed")

    def test_endless_challenge_is_error_not_claimed(self):
        # v1 bug: exhausting all retries on a challenge page was reported as "claimed".
        result, driver, _ = self.check([[CHALLENGE]] * 5)
        self.assertEqual((result, driver.gets), ("error", gunslol.MAX_RETRIES))

    def test_endless_rate_limit_is_error(self):
        self.assertEqual(self.check([[RATE_LIMIT]] * 5)[0], "error")

    def test_rate_limit_rotates_proxy_then_succeeds(self):
        rotator = mock.Mock()
        driver = FakeDriver([[RATE_LIMIT], [NOT_FOUND]])
        result = gunslol.check_username(FakeBrowser(driver), "efe", rotator)
        self.assertEqual(result, "unclaimed")
        rotator.rotate.assert_called_once()

    def test_page_load_timeout_still_reads_dom(self):
        self.assertEqual(self.check([("timeout", [NOT_FOUND])])[0], "unclaimed")

    def test_network_errors_are_retried(self):
        error = WebDriverException("unknown error: net::ERR_PROXY_CONNECTION_FAILED")
        result, driver, _ = self.check([error, error, [CLAIMED]])
        self.assertEqual((result, driver.gets), ("claimed", 3))

    def test_dead_browser_is_restarted(self):
        result, _, browser = self.check([WebDriverException("invalid session id"), [CLAIMED]], dead=True)
        self.assertEqual((result, browser.restarts), ("claimed", 1))


# --------------------------------------------------------------------------- runner

class RunnerDriver:
    """Fake site: 'taken*' names are claimed, 'flaky' fails its first 3 loads, others are free."""

    loads = {}

    def get(self, url):
        name = url.rsplit("/", 1)[1]
        RunnerDriver.loads[name] = RunnerDriver.loads.get(name, 0) + 1
        if name.startswith("taken"):
            self.page = CLAIMED
        elif name == "flaky" and RunnerDriver.loads[name] <= gunslol.MAX_RETRIES:
            self.page = CHALLENGE
        else:
            self.page = NOT_FOUND

    def execute_script(self, script):
        return None if "window.stop" in script else self.page


class FakeSession:
    def __init__(self, proxy_port=None):
        self.driver = None
        self.version = "141.0.7390.54"

    def start(self):
        self.driver = RunnerDriver()
        return self

    def is_dead(self, exc=None):
        return False

    def restart(self):
        pass

    def mark_healthy(self):
        pass

    def quit(self, hard=False):
        self.driver = None


class RunnerTests(unittest.TestCase):
    def test_custom_list_end_to_end(self):
        RunnerDriver.loads = {}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gunslol, "UNCLAIMED_FILE", os.path.join(tmp, "unclaimed.txt")), \
                mock.patch.object(gunslol, "BrowserSession", FakeSession), \
                mock.patch.object(gunslol, "CHALLENGE_WAIT", 0.05), \
                mock.patch.object(gunslol, "POLL_INTERVAL", 0.01), \
                mock.patch.object(gunslol, "_sleep", lambda seconds: None), \
                mock.patch.object(gunslol, "set_console_title", lambda text: None), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            stats = gunslol.check_user_status(
                None, 0, customlist=["taken1", "free1", ".prem", "flaky", "free2"],
                filter_premium=True, save_to_file=True, use_http=False)
            with open(os.path.join(tmp, "unclaimed.txt"), encoding="utf-8") as handle:
                saved = handle.read().split()
        self.assertEqual((stats.checked, stats.unclaimed, stats.claimed, stats.errors, stats.skipped),
                         (4, 3, 1, 0, 1))
        self.assertEqual(saved, ["free1", "free2", "flaky"])
        text = output.getvalue()
        self.assertIn("will retry at the end", text)
        self.assertIn("Retrying 1 username(s)", text)
        self.assertIn("Customlist check completed. 4 usernames checked.", text)


class MultiSiteRunnerTests(unittest.TestCase):
    def test_checks_each_site_and_prefixes_saved_hits(self):
        guns, haunt, jugs = gunslol.BUILTIN_SITES
        calls = []

        def fake_check(browser, username, rotator=None, site=None):
            calls.append(site.name)
            return "unclaimed" if site.name == "haunt.gg" else "claimed"

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gunslol, "UNCLAIMED_FILE", os.path.join(tmp, "unclaimed.txt")), \
                mock.patch.object(gunslol, "BrowserSession", FakeSession), \
                mock.patch.object(gunslol, "check_username", side_effect=fake_check), \
                mock.patch.object(gunslol, "_sleep", lambda seconds: None), \
                mock.patch.object(gunslol, "set_console_title", lambda text: None), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            stats = gunslol.check_user_status(
                None, 0, customlist=["free1", ".prem"], filter_premium=True,
                save_to_file=True, sites=[guns, haunt, jugs], use_http=False)
            with open(os.path.join(tmp, "unclaimed.txt"), encoding="utf-8") as handle:
                saved = handle.read().split()
        # .prem is only skipped on guns.lol (the only site with premium aliases);
        # it is a valid name elsewhere, so haunt.gg is still checked.
        self.assertEqual(calls, ["guns.lol", "haunt.gg", "jugs.lol", "haunt.gg", "jugs.lol"])
        self.assertEqual(saved, ["haunt.gg/free1", "haunt.gg/.prem"])
        self.assertEqual((stats.checked, stats.site_checks, stats.unclaimed, stats.claimed,
                          stats.skipped, stats.errors), (2, 5, 2, 3, 1, 0))
        self.assertEqual(stats.per_site["guns.lol"]["skipped"], 1)
        self.assertIn("haunt.gg/free1", output.getvalue())


# --------------------------------------------------------------------------- generator

class ListGeneratorTests(unittest.TestCase):
    def test_every_pattern_generates(self):
        for pattern in listgen.PATTERNS:
            result = listgen.generate(pattern["code"], limit=3000, filter_premium=False)
            self.assertTrue(result.count > 0, pattern["code"])
            for name in result.names:
                self.assertLessEqual(len(name), listgen.MAX_USERNAME_LENGTH, pattern["code"])
                self.assertRegex(name, r"^[a-z0-9._-]+$")

    def test_known_counts(self):
        self.assertEqual(listgen.generate("2c").count, 36 ** 2)
        self.assertEqual(listgen.generate("3c").count, 36 ** 3)
        self.assertEqual(listgen.generate("2l").count, 26 ** 2)
        self.assertEqual(listgen.generate("2n").count, 100)
        self.assertEqual(listgen.generate("4n").count, 10000)
        self.assertEqual(listgen.generate("semi3l").count, 26 ** 3)
        self.assertEqual(listgen.generate("rep4l").count, 10000)
        self.assertGreaterEqual(listgen.generate("pron4l").count, 20000)
        self.assertGreaterEqual(listgen.generate("faces").count, 600)
        self.assertGreaterEqual(listgen.generate("cities").count, 600)
        self.assertGreaterEqual(listgen.generate("egirl").count, 150)
        self.assertGreaterEqual(listgen.generate("eboy").count, 150)
        self.assertGreaterEqual(listgen.generate("leet4c").count, 1000)

    def test_aliases_and_resolution(self):
        self.assertEqual(listgen.resolve_pattern("All 3Cs")["code"], "all3c")
        self.assertEqual(listgen.resolve_pattern("all 2cs")["code"], "2c" if False else "all2c")
        self.assertEqual(listgen.resolve_pattern("top 10k")["code"], "top10k")
        self.assertEqual(listgen.resolve_pattern("3L")["code"], "3l")
        self.assertIsNone(listgen.resolve_pattern("not a pattern"))

    def test_premium_filter_and_normalisation(self):
        result = listgen.generate("uscore", limit=5000, filter_premium=True)
        for name in result.names:
            self.assertFalse(listgen.is_premium_alias(name), name)
        self.assertEqual(listgen.normalize(" Santa Cruz "), "santacruz")
        self.assertEqual(listgen.normalize("bad name!"), "badname")
        self.assertEqual(listgen.normalize("x" * 40), "")

    def test_write_list_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "list.txt")
            mode, lines = listgen.write_list(["aa", "bb"], path, "replace")
            self.assertEqual((mode, lines), ("replaced", 2))
            mode, lines = listgen.write_list(["cc"], path, "append")
            self.assertEqual((mode, lines), ("appended", 1))
            with open(path, encoding="utf-8") as handle:
                body = [line for line in handle.read().split("\n")
                        if line.strip() and not line.startswith("//")]
            self.assertEqual(body, ["aa", "bb", "cc"])
            mode, _ = listgen.write_list(["dd"], path, "replace")
            self.assertEqual(mode, "replaced")

    def test_mix_dedupes(self):
        combined = listgen.mix(["2c", "all2c"], limit=5000)
        self.assertEqual(len(combined.names), len(set(combined.names)))


# --------------------------------------------------------------------------- settings & menus

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def test_defaults_when_missing(self):
        settings = gunslol.load_settings(self.path)
        self.assertEqual(settings["intro"], "fade")
        self.assertEqual(settings["theme"], "purple")
        self.assertEqual(settings["sites"], ["guns.lol"])
        self.assertTrue(settings["filter_premium"])

    def test_round_trip_and_sanitising(self):
        settings = gunslol.DEFAULT_SETTINGS.copy()
        settings.update({"intro": "decode", "theme": "ocean", "letter_count": 7,
                         "delay": 1.5, "sites": ["jugs.lol", "guns.lol"]})
        self.assertTrue(gunslol.save_settings(settings, self.path))
        loaded = gunslol.load_settings(self.path)
        self.assertEqual(loaded["intro"], "decode")
        self.assertEqual(loaded["theme"], "ocean")
        self.assertEqual(loaded["letter_count"], 7)
        self.assertEqual(loaded["delay"], 1.5)
        # Invalid values fall back to defaults instead of breaking a run.
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"intro": "nope", "theme": 42, "delay": "inf", "letter_count": 99,'
                         ' "sites": "guns.lol"}')
        loaded = gunslol.load_settings(self.path)
        self.assertEqual(loaded["intro"], "fade")
        self.assertEqual(loaded["theme"], "purple")
        self.assertEqual(loaded["delay"], 0)
        self.assertEqual(loaded["letter_count"], 4)
        self.assertEqual(loaded["sites"], ["guns.lol"])

    def test_themes_and_animations_registry(self):
        self.assertTrue(gunslol.apply_theme("ocean"))
        self.assertEqual(gunslol.current_theme(), "ocean")
        self.assertFalse(gunslol.apply_theme("does-not-exist"))
        self.assertEqual(gunslol.current_theme(), "ocean")
        gunslol.apply_theme("purple")
        for name, entry in gunslol.INTRO_ANIMATIONS.items():
            if name != "none":
                self.assertTrue(callable(entry["painter"]), name)
        self.assertIn("fade", gunslol.INTRO_ANIMATIONS)
        self.assertIn("decode", gunslol.INTRO_ANIMATIONS)

    def test_index_parser_ranges(self):
        self.assertEqual(gunslol._parse_indices("2-3,1", 4), [0, 1, 2])
        self.assertEqual(gunslol._parse_indices("3-2", 4), [1, 2])
        with self.assertRaises(ValueError):
            gunslol._parse_indices("x", 4)



# --------------------------------------------------------------------------- speed engine

class SpeedHandler(http.server.BaseHTTPRequestHandler):
    """Mock site: /taken* -> 200 profile, /busy -> 429, default -> 404 free."""
    mode = {"name": "normal"}
    seen = 0

    def do_GET(self):
        SpeedHandler.seen += 1
        name = self.path.strip("/")
        if name == "challenge":
            self._send(403, "<html><title>Just a moment...</title></html>")
        elif name == "busy":
            self._send(429, "<html><title>429 Too Many Requests</title></html>")
        elif name.startswith("taken"):
            self._send(200, "<html><title>%s | mock.lol</title><h1>%s</h1></html>" % (name, name))
        else:
            self._send(404, "<html><title>mock.lol</title><h1>Username not found</h1>"
                            "<p>Claim this username</p></html>")

    def _send(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class SpeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SpeedHandler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _site(self, **kwargs):
        options = {"name": "mock.lol", "url": self.base + "/{username}",
                   "not_found": ("username not found",), "available": ("claim this username",),
                   "fast_http": True, "fast_http_claimed": True}
        options.update(kwargs)
        return gunslol.Site(**options)

    def test_probe_classifies_without_browser(self):
        site = self._site()
        probe = fastpath.SiteProbe(site)
        self.assertEqual(probe.check("taken1").verdict, "claimed")
        self.assertEqual(probe.check("free1").verdict, "unclaimed")
        self.assertEqual(probe.check("busy").verdict, "ratelimited")
        self.assertEqual(probe.check("challenge").verdict, "challenge")

    def test_probe_reports_unknown_instead_of_guessing(self):
        # A 200 page with no usable marker must be escalated to the browser.
        site = self._site(not_found=(), available=(), fast_http_claimed=False)
        self.assertIsNone(fastpath.SiteProbe(site).check("taken1").verdict)

    def test_pacer_backs_off_and_recovers(self):
        pacer = fastpath.Pacer("adaptive")
        start = pacer.interval("mock.lol")
        pacer.record("mock.lol", "ratelimited")
        after_limit = pacer.interval("mock.lol")
        self.assertGreater(after_limit, start)
        for _ in range(8):
            pacer.record("mock.lol", "claimed")
        self.assertLess(pacer.interval("mock.lol"), after_limit)
        state = pacer.state("mock.lol")
        self.assertEqual((state.limited, state.ok), (1, 8))

    def test_pacer_spaces_requests(self):
        pacer = fastpath.Pacer("steady")
        first = time.monotonic()
        pacer.acquire("x")
        second = time.monotonic()
        pacer.acquire("x")
        third = time.monotonic()
        self.assertLess(second - first, 0.1)
        self.assertGreaterEqual(third - second, pacer.minimum * 0.8)

    def test_fast_engine_skips_non_http_sites(self):
        browser_site = gunslol.Site("browser.lol", "https://browser.lol/{username}")
        engine = fastpath.FastEngine([self._site(), browser_site])
        self.assertEqual(engine.sites_for_http(), ["mock.lol"])
        self.assertEqual(engine.skipped, ["browser.lol"])
        result = engine.check(self._site(), "free2")
        self.assertEqual(result.verdict, "unclaimed")
        self.assertEqual(engine.pacer.state("mock.lol").requests, 1)

    def test_benchmark_stops_at_first_pushback(self):
        limits_before = SpeedHandler.seen
        report = fastpath.benchmark(fastpath.Pacer("steady"), self._site(url=self.base + "/{username}"),
                                    max_requests=8, start_interval=0.1, floor=0.05)
        self.assertLessEqual(report.requests, 8)
        self.assertTrue(report.stopped_by or report.limited or report.clean)
        self.assertGreaterEqual(SpeedHandler.seen, limits_before)
        self.assertEqual(report.lines()[0].startswith("mock.lol"), True)
        self.assertTrue(fastpath.tune_advice(report))


class HttpRunnerTests(unittest.TestCase):
    def test_mixed_sites_use_http_then_browser(self):
        import types
        http_site = types.SimpleNamespace(
            name="mock.lol", label="mock.lol", premium=False,
            profile_url=lambda name: "https://mock.lol/" + name,
            display=lambda name: "mock.lol/" + name, fast_http=True, fast_http_claimed=True)
        browser_site = gunslol.Site("browser.lol", "https://browser.lol/{username}")
        calls = []

        class FakeEngine:
            workers = 2
            def __init__(self, sites, proxies, speed, interval):
                self.probes = {"mock.lol": object()}
                self.skipped = ["browser.lol"]
                self.pacer = mock.Mock()
                self.pacer.workers = 2
                self.pacer.state.return_value.average_latency = 0.1
            def sites_for_http(self):
                return ["mock.lol"]
            def enabled(self):
                return True
            def check(self, site, username):
                return fastpath.ProbeResult("unclaimed", 404, 0.05, "HTTP 404")

        def fake_browser_check(browser, username, rotator=None, site=None):
            calls.append(site.name)
            return "claimed"

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gunslol, "UNCLAIMED_FILE", os.path.join(tmp, "u.txt")), \
                mock.patch.object(gunslol, "BrowserSession", FakeSession), \
                mock.patch.object(gunslol, "FastEngine", FakeEngine, create=True), \
                mock.patch.object(gunslol.fastpath, "FastEngine", FakeEngine), \
                mock.patch.object(gunslol, "check_username", side_effect=fake_browser_check), \
                mock.patch.object(gunslol, "_sleep", lambda seconds: None), \
                mock.patch.object(gunslol, "set_console_title", lambda text: None), \
                contextlib.redirect_stdout(io.StringIO()):
            stats = gunslol.check_user_status(
                None, 0, customlist=["abc"], save_to_file=True,
                sites=[http_site, browser_site], speed="adaptive", use_http=True)
        self.assertEqual(calls, ["browser.lol"])
        self.assertEqual((stats.http_checks, stats.browser_checks, stats.checked), (1, 1, 1))
        self.assertEqual(stats.unclaimed, 1)
        self.assertEqual(stats.claimed, 1)


class IntroRenderTests(unittest.TestCase):
    def test_every_animation_renders_ansi_frames(self):
        import re

        class FakeOut(io.StringIO):
            def isatty(self):
                return True

            def flush(self):
                pass

        real_stdout = sys.stdout
        real_size, real_enter = gunslol.terminal_size, gunslol._enter_pressed
        stdin = sys.stdin

        class FakeIn(io.StringIO):
            def isatty(self):
                return True

        sys.stdin = FakeIn("\n")
        try:
            for name in gunslol.INTRO_ANIMATIONS:
                buffer = FakeOut()
                sys.stdout = buffer
                gunslol.terminal_size = lambda: (120, 40)
                state = {"frames": 0}

                def enter(timeout, state=state):
                    state["frames"] += 1
                    return state["frames"] >= 5

                gunslol._enter_pressed = enter
                gunslol.play_intro(name)
                text = buffer.getvalue()
                self.assertIn("\x1b[38;2;", text, name)
                self.assertIn("\x1b[J", text, name)
                # No raw rgb triples may leak as text (the v2.2 intro bug).
                stripped = re.sub(r"\x1b\[[0-9;]*m", "", text)
                self.assertIsNone(re.search(r"(?<!\d)\d{1,3};\d{1,3};\d{1,3}(?!\d)", stripped), name)
        finally:
            sys.stdout, gunslol.terminal_size = real_stdout, real_size
            gunslol._enter_pressed, sys.stdin = real_enter, stdin

    def test_tiny_and_wide_terminals(self):
        for size in ((40, 14), (30, 10), (200, 60)):
            self.assertGreaterEqual(gunslol._top_pad(size[1], 5), 0, size)


# --------------------------------------------------------------------------- prompts & misc

class PromptTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(gunslol, "show_header", lambda: None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_invalid_answers_show_error_then_accept(self):
        with mock.patch("builtins.input", side_effect=["abc", "0", "5"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            value = gunslol.get_input("len: ", type_=int, validation=lambda x: x > 0, error_msg="Bad value!")
        self.assertEqual(value, 5)
        self.assertEqual(output.getvalue().count("Bad value!"), 2)

    def test_yes_no(self):
        with mock.patch("builtins.input", side_effect=["maybe", "YES"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(gunslol.get_input("?", type_=bool))

    def test_eof_no_longer_loops_forever(self):
        with mock.patch("builtins.input", side_effect=EOFError), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(gunslol.InputClosed):
                gunslol.get_input("?", type_=bool)


class MiscTests(unittest.TestCase):
    def test_user_agent_matches_chrome_version(self):
        user_agent, metadata, _ = gunslol.build_user_agent("141.0.7390.54")
        self.assertIn("Chrome/141.0.0.0", user_agent)
        self.assertNotIn("Headless", user_agent)
        self.assertEqual(metadata["brands"][0]["version"], "141")
        self.assertEqual(metadata["fullVersionList"][0]["version"], "141.0.7390.54")

    def test_banner(self):
        wide = gunslol.build_banner(200, with_prompt=True)
        self.assertIn("> Press Enter", wide)
        self.assertIn(gunslol.REPO_URL, wide)
        self.assertIn("|____/", wide)  # new BIOSCOUT banner
        compact = gunslol.build_banner(60)
        self.assertIn("github.com/efekrbas/guns.lol-username-checker", compact)
        self.assertTrue(all(len(line) <= 60 for line in compact.splitlines()))


if __name__ == "__main__":
    unittest.main()
