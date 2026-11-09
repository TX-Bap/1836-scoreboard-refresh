#!/usr/bin/env python3
"""Local checks for scoreboard refresh validation. Never calls the dashboard host."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_call  # noqa: E402

FIXTURE_TOKEN = "fixture-bearer-token"
PRODUCTION_HOST = "lagoon-brook-orchid-pearl.grok.me"
_REAL_GETADDRINFO = socket.getaddrinfo


def _guard_getaddrinfo(host, *args, **kwargs):
    normalized = host.strip("[]").lower() if isinstance(host, str) else host
    if normalized not in {"127.0.0.1", "localhost", "::1"}:
        raise AssertionError(f"test tried to resolve a non-loopback host: {host}")
    return _REAL_GETADDRINFO(host, *args, **kwargs)


socket.getaddrinfo = _guard_getaddrinfo


def make_config(**overrides) -> refresh_call.Config:
    values = {
        "profile": "sales",
        "path": "/api/refresh-sales",
        "token": FIXTURE_TOKEN,
        "token_source": "SCOREBOARD_REFRESH_TOKEN",
        "base_url": "http://127.0.0.1:9",
        "timeout": 5,
        "max_attempts": 3,
        "backoff": (10, 30),
        "event": "workflow_dispatch",
        "require_chicago_hour": None,
        "now": None,
    }
    values.update(overrides)
    return refresh_call.Config(**values)


def assert_no_token(testcase: unittest.TestCase, text: str) -> None:
    testcase.assertNotIn(FIXTURE_TOKEN, text)
    testcase.assertNotIn("Bearer " + FIXTURE_TOKEN, text)


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, token, timeout):
        self.calls.append({"url": url, "token": token, "timeout": timeout})
        if not self.responses:
            raise AssertionError("transport called more times than scripted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ValidationTests(unittest.TestCase):
    def test_sales_success_requires_boolean_ok_and_json_type(self):
        summary = refresh_call.evaluate_response(
            200,
            "application/json; charset=utf-8",
            json.dumps(
                {
                    "ok": True,
                    "businessDate": "2026-11-09",
                    "updatedAt": "2026-11-09T12:00:00Z",
                    "lastError": None,
                }
            ).encode(),
            "sales",
            [],
        )
        self.assertIn("businessDate=2026-11-09", summary)
        self.assertIn("lastError=null", summary)

    def test_sales_accepts_ok_true_without_other_fields(self):
        summary = refresh_call.evaluate_response(
            200, "Application/JSON", b'{"ok": true}', "sales", []
        )
        self.assertEqual(summary, "ok=true")

    def test_string_and_numeric_ok_are_failures(self):
        for body in (b'{"ok": "true"}', b'{"ok": 1}', b'{"ok": false}', b"{}"):
            with self.subTest(body=body):
                with self.assertRaises(refresh_call.RefreshError) as caught:
                    refresh_call.evaluate_response(200, "application/json", body, "sales", [])
                self.assertFalse(caught.exception.retry)
                self.assertIn("boolean true", caught.exception.reason)

    def test_html_200_is_permanent_failure(self):
        for content_type, body in (
            ("text/html; charset=utf-8", b"<!DOCTYPE html><html><body>sign in</body></html>"),
            ("application/json", b"<!DOCTYPE html><html>nope</html>"),
            ("text/html", b'{"ok": true}'),
        ):
            with self.subTest(content_type=content_type):
                with self.assertRaises(refresh_call.RefreshError) as caught:
                    refresh_call.evaluate_response(200, content_type, body, "sales", [])
                self.assertFalse(caught.exception.retry)
                self.assertIn("HTML", caught.exception.reason)

    def test_non_json_content_type_is_permanent(self):
        with self.assertRaises(refresh_call.RefreshError) as caught:
            refresh_call.evaluate_response(200, "text/plain", b'{"ok": true}', "sales", [])
        self.assertFalse(caught.exception.retry)
        self.assertIn("application/json", caught.exception.reason)
        self.assertIn("text/plain", caught.exception.reason)

    def test_missing_content_type_is_permanent(self):
        with self.assertRaises(refresh_call.RefreshError) as caught:
            refresh_call.evaluate_response(200, "", b'{"ok": true}', "sales", [])
        self.assertIn("(missing)", caught.exception.reason)
        self.assertFalse(caught.exception.retry)

    def test_invalid_json_is_permanent(self):
        with self.assertRaises(refresh_call.RefreshError) as caught:
            refresh_call.evaluate_response(200, "application/json", b"{", "sales", [])
        self.assertIn("not valid JSON", caught.exception.reason)
        self.assertFalse(caught.exception.retry)

    def test_status_classification(self):
        cases = {
            401: False,
            403: False,
            404: False,
            409: False,
            301: False,
            302: False,
            400: False,
            204: False,
            408: True,
            429: True,
            500: True,
            502: True,
            503: True,
            504: True,
        }
        for status, retry in cases.items():
            with self.subTest(status=status):
                with self.assertRaises(refresh_call.RefreshError) as caught:
                    refresh_call.evaluate_response(
                        status, "application/json", b'{"ok": false}', "sales", []
                    )
                self.assertEqual(caught.exception.retry, retry)
                self.assertEqual(caught.exception.status, status)

    def test_excerpt_redacts_token_and_bearer_header(self):
        body = json.dumps(
            {"ok": False, "lastError": f"echo {FIXTURE_TOKEN}", "note": "Bearer leaked-value"}
        ).encode()
        with self.assertRaises(refresh_call.RefreshError) as caught:
            refresh_call.evaluate_response(
                200, "application/json", body, "sales", [FIXTURE_TOKEN]
            )
        self.assertNotIn(FIXTURE_TOKEN, caught.exception.excerpt)
        self.assertNotIn("leaked-value", caught.exception.excerpt)
        self.assertIn("[redacted]", caught.exception.excerpt)

    def test_kpi_contract_success_and_rejections(self):
        payload = {
            "ok": True,
            "builtAt": "2026-11-09T12:05:00Z",
            "weeksUpdated": 1,
            "sources": {
                "toast": {
                    "lastSuccess": "2026-11-09T12:00:00Z",
                    "coverage": "2026-11-03/2026-11-09",
                    "stale": False,
                }
            },
            "lastError": None,
        }
        summary = refresh_call.evaluate_response(
            200,
            "application/json",
            json.dumps(payload).encode(),
            "kpi",
            [],
        )
        self.assertIn("builtAt=2026-11-09T12:05:00Z", summary)
        self.assertIn("weeksUpdated=1", summary)
        self.assertIn("source toast:", summary)
        self.assertIn("stale=false", summary)

        payload["sources"]["toast"]["lastSuccess"] = None
        payload["sources"]["toast"]["stale"] = True
        payload["weeksUpdated"] = 0
        payload["lastError"] = "partial"
        summary = refresh_call.evaluate_response(
            200, "application/json", json.dumps(payload).encode(), "kpi", []
        )
        self.assertIn("stale=true", summary)
        self.assertIn("lastSuccess=null", summary)

        rejections = [
            ("builtAt", "2026-11-09"),
            ("weeksUpdated", True),
            ("weeksUpdated", "1"),
            ("weeksUpdated", -1),
            ("sources", []),
        ]
        for key, value in rejections:
            bad = json.loads(json.dumps(payload))
            bad[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(refresh_call.RefreshError) as caught:
                    refresh_call.evaluate_response(
                        200, "application/json", json.dumps(bad).encode(), "kpi", []
                    )
                self.assertFalse(caught.exception.retry)

        missing = json.loads(json.dumps(payload))
        del missing["sources"]["toast"]["coverage"]
        with self.assertRaises(refresh_call.RefreshError) as caught:
            refresh_call.evaluate_response(
                200, "application/json", json.dumps(missing).encode(), "kpi", []
            )
        self.assertIn("coverage", caught.exception.reason)

    def test_empty_kpi_sources_are_allowed(self):
        payload = {
            "ok": True,
            "builtAt": "2026-11-09T12:05:00+00:00",
            "weeksUpdated": 0,
            "sources": {},
            "lastError": None,
        }
        summary = refresh_call.evaluate_response(
            200, "application/json", json.dumps(payload).encode(), "kpi", []
        )
        self.assertIn("weeksUpdated=0", summary)


class ExecuteTests(unittest.TestCase):
    def test_retries_transient_status_with_backoff_then_succeeds(self):
        transport = RecordingTransport(
            [
                (502, "application/json", b'{"ok": false, "lastError": "pull failed"}'),
                (503, "text/plain", b"unavailable"),
                (200, "application/json", b'{"ok": true, "businessDate": "2026-11-09"}'),
            ]
        )
        delays = []
        outcome = refresh_call.execute(make_config(), transport, delays.append)
        self.assertEqual(outcome.kind, "success")
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(delays, [10, 30])
        self.assertEqual(len(transport.calls), 3)
        log = "\n".join(outcome.logs)
        self.assertIn("transient HTTP 502", log)
        self.assertIn("Waiting 10 seconds", log)
        self.assertIn("Waiting 30 seconds", log)
        self.assertIn("businessDate=2026-11-09", outcome.message)
        assert_no_token(self, log + outcome.message)

    def test_permanent_http_and_html_are_not_retried(self):
        for response, needle in (
            ((401, "application/json", b'{"ok": false}'), "HTTP 401"),
            ((200, "text/html", b"<html>login</html>"), "HTML"),
            ((200, "application/json", b'{"ok": false}'), "boolean true"),
        ):
            with self.subTest(needle=needle):
                transport = RecordingTransport([response, (200, "application/json", b'{"ok": true}')])
                delays = []
                outcome = refresh_call.execute(make_config(), transport, delays.append)
                self.assertEqual(outcome.kind, "failed")
                self.assertEqual(outcome.attempts, 1)
                self.assertEqual(delays, [])
                self.assertEqual(len(transport.calls), 1)
                self.assertIn(needle, outcome.message + "\n" + "\n".join(outcome.logs))

    def test_network_errors_retry_and_token_is_removed_from_the_message(self):
        transport = RecordingTransport(
            [
                refresh_call.RefreshError(
                    f"network error: refused while using {FIXTURE_TOKEN}",
                    retry=True,
                ),
                (200, "application/json", b'{"ok": true}'),
            ]
        )
        delays = []
        outcome = refresh_call.execute(
            make_config(backoff=(5, 15)), transport, delays.append
        )
        self.assertEqual(outcome.kind, "success")
        self.assertEqual(delays, [5])
        assert_no_token(self, "\n".join(outcome.logs) + outcome.message)
        self.assertIn("[redacted]", "\n".join(outcome.logs))

    def test_kpi_path_skip_and_hour_gate_do_not_call(self):
        def boom(url, token, timeout):
            raise AssertionError(f"unexpected call to {url}")

        skipped = refresh_call.execute(
            make_config(
                profile="kpi",
                path="  ",
                token="",
                event="schedule",
                require_chicago_hour=6,
                now=datetime(2026, 1, 15, 12, 5, tzinfo=timezone.utc),
            ),
            boom,
            lambda delay: None,
        )
        self.assertEqual(skipped.kind, "skipped")
        self.assertIn("KPI_REFRESH_PATH is unset", skipped.message)
        self.assertIn("/api/refresh-kpis", skipped.message)
        self.assertIn("No request was sent", skipped.message)

        outside = refresh_call.execute(
            make_config(
                profile="kpi",
                path="",
                token="",
                event="schedule",
                require_chicago_hour=6,
                now=datetime(2026, 1, 15, 11, 5, tzinfo=timezone.utc),
            ),
            boom,
            lambda delay: None,
        )
        self.assertEqual(outside.kind, "skipped")
        self.assertIn("America/Chicago", outside.message)
        self.assertIn("05:05", outside.message)

        winter = refresh_call.execute(
            make_config(
                profile="kpi",
                path="/api/refresh-kpis",
                event="schedule",
                require_chicago_hour=6,
                now=datetime(2026, 1, 15, 12, 5, tzinfo=timezone.utc),
            ),
            RecordingTransport([(200, "application/json", self._kpi_body())]),
            lambda delay: None,
        )
        self.assertEqual(winter.kind, "success")

        summer_outside = refresh_call.execute(
            make_config(
                profile="kpi",
                path="/api/refresh-kpis",
                token="",
                event="schedule",
                require_chicago_hour=6,
                now=datetime(2026, 7, 15, 12, 5, tzinfo=timezone.utc),
            ),
            boom,
            lambda delay: None,
        )
        self.assertEqual(summer_outside.kind, "skipped")
        self.assertIn("07:05", summer_outside.message)

        manual = refresh_call.execute(
            make_config(
                profile="kpi",
                path="/api/refresh-kpis",
                event="workflow_dispatch",
                require_chicago_hour=6,
                now=datetime(2026, 1, 15, 11, 5, tzinfo=timezone.utc),
            ),
            RecordingTransport([(200, "application/json", self._kpi_body())]),
            lambda delay: None,
        )
        self.assertEqual(manual.kind, "success")

    def test_missing_token_fails_before_a_call(self):
        def boom(url, token, timeout):
            raise AssertionError("request should not be sent")

        with self.assertRaises(refresh_call.RefreshError) as caught:
            refresh_call.execute(
                make_config(token="", token_source="SCOREBOARD_REFRESH_TOKEN"),
                boom,
                lambda delay: None,
            )
        self.assertIn("SCOREBOARD_REFRESH_TOKEN", caught.exception.reason)
        self.assertIn("unset", caught.exception.reason)

    def test_rejected_paths_do_not_echo_the_input(self):
        for path in ("/api/../refresh-sales", "https://example.com/api/refresh-kpis", "/api/refresh?token=abc"):
            with self.subTest(path=path):
                with self.assertRaises(refresh_call.RefreshError) as caught:
                    refresh_call.normalize_path(path, "kpi")
                self.assertNotIn("example.com", caught.exception.reason)
                self.assertNotIn("token=abc", caught.exception.reason)
                self.assertIn("<rejected-path>", caught.exception.reason)

    def test_base_url_stays_on_the_dashboard_host_or_loopback(self):
        self.assertEqual(
            refresh_call.validate_base_url(f"https://{PRODUCTION_HOST}", True),
            f"https://{PRODUCTION_HOST}",
        )
        with self.assertRaises(refresh_call.RefreshError):
            refresh_call.validate_base_url(f"https://{PRODUCTION_HOST}", False)
        for url in (
            "https://example.com",
            f"https://user:secret@{PRODUCTION_HOST}",
            f"https://{PRODUCTION_HOST}/api/refresh-sales",
            f"https://{PRODUCTION_HOST}?token=abc",
            "http://example.com",
        ):
            with self.subTest(url=url):
                with self.assertRaises(refresh_call.RefreshError):
                    refresh_call.validate_base_url(url, True)
        self.assertEqual(
            refresh_call.validate_base_url("http://127.0.0.1:9", False),
            "http://127.0.0.1:9",
        )

    def test_parser_has_no_token_value_argument(self):
        dests = {action.dest for action in refresh_call.build_parser()._actions}
        self.assertIn("token_env", dests)
        self.assertNotIn("token", dests)
        backoff = next(
            action for action in refresh_call.build_parser()._actions if action.dest == "backoff_seconds"
        )
        self.assertEqual(backoff.default, (10, 30))

    @staticmethod
    def _kpi_body() -> bytes:
        return json.dumps(
            {
                "ok": True,
                "builtAt": "2026-11-09T12:05:00Z",
                "weeksUpdated": 1,
                "sources": {
                    "toast": {
                        "lastSuccess": "2026-11-09T12:00:00Z",
                        "coverage": "2026-11-03/2026-11-09",
                        "stale": False,
                    }
                },
                "lastError": None,
            }
        ).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return self.client_address[0]

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "accept": self.headers.get("Accept"),
                "method": "POST",
            }
        )
        scenario = self.server.scenarios.pop(0) if self.server.scenarios else {
            "status": 500,
            "content_type": "text/plain",
            "body": b"no scenario",
        }
        delay = scenario.get("delay", 0)
        if delay:
            time.sleep(delay)
        body = scenario.get("body", b"")
        try:
            self.send_response(scenario["status"])
            self.send_header("Content-Type", scenario.get("content_type", "application/json"))
            if scenario.get("location"):
                self.send_header("Location", scenario["location"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt, *args):
        return


class LoopbackServer:
    def __init__(self, scenarios):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.scenarios = list(scenarios)
        self.httpd.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return self.httpd.requests

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class CliTests(unittest.TestCase):
    def run_cli(self, args, env_extra=None):
        if "--allow-remote" in args or PRODUCTION_HOST in " ".join(args):
            raise AssertionError("tests must not call the dashboard host")
        env = os.environ.copy()
        env.pop("SCOREBOARD_REFRESH_TOKEN", None)
        env.pop("SCOREBOARD_KPI_REFRESH_TOKEN", None)
        env["GITHUB_ACTIONS"] = "false"
        if env_extra:
            env.update(env_extra)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "refresh_call.py"), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        combined = completed.stdout + completed.stderr
        self.assertNotIn(FIXTURE_TOKEN, combined)
        self.assertNotIn("fallback-fixture-token", combined)
        return completed

    def test_live_loopback_success_retry_redirect_and_timeout(self):
        success = {
            "status": 200,
            "content_type": "application/json; charset=utf-8",
            "body": b'{"ok": true, "businessDate": "2026-11-09", "lastError": null}',
        }
        server = LoopbackServer(
            [
                {"status": 502, "content_type": "application/json", "body": b'{"ok": false}'},
                success,
            ]
        )
        self.addCleanup(server.close)
        completed = self.run_cli(
            [
                "--profile",
                "sales",
                "--path",
                "/api/refresh-sales",
                "--token-env",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                server.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "3",
                "--backoff-seconds",
                "0,0",
            ],
            {"SCOREBOARD_REFRESH_TOKEN": FIXTURE_TOKEN},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual([item["path"] for item in server.requests], ["/api/refresh-sales", "/api/refresh-sales"])
        self.assertEqual(server.requests[0]["auth"], f"Bearer {FIXTURE_TOKEN}")
        self.assertEqual(server.requests[0]["accept"], "application/json")
        self.assertIn("Token source: SCOREBOARD_REFRESH_TOKEN", completed.stdout)
        self.assertIn("Attempt 2/3: HTTP 200", completed.stdout)
        self.assertIn("transient HTTP 502", completed.stdout)

        html = LoopbackServer(
            [
                {
                    "status": 200,
                    "content_type": "text/html",
                    "body": b"<!DOCTYPE html><html>login</html>",
                },
                success,
            ]
        )
        self.addCleanup(html.close)
        html_run = self.run_cli(
            [
                "--profile",
                "sales",
                "--path",
                "/api/refresh-sales",
                "--token-env",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                html.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "3",
                "--backoff-seconds",
                "0,0",
            ],
            {"SCOREBOARD_REFRESH_TOKEN": FIXTURE_TOKEN, "GITHUB_ACTIONS": "true"},
        )
        self.assertEqual(html_run.returncode, 1)
        self.assertEqual(len(html.requests), 1)
        self.assertIn("HTML", html_run.stderr)
        self.assertIn("::error::", html_run.stderr)
        self.assertNotIn(FIXTURE_TOKEN, html_run.stdout + html_run.stderr)

        redirected = LoopbackServer(
            [
                {
                    "status": 302,
                    "content_type": "text/html",
                    "location": "/api/should-not-follow",
                    "body": b"<html>moved</html>",
                },
                success,
            ]
        )
        self.addCleanup(redirected.close)
        redirect_run = self.run_cli(
            [
                "--profile",
                "sales",
                "--path",
                "/api/refresh-sales",
                "--token-env",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                redirected.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "2",
                "--backoff-seconds",
                "0",
            ],
            {"SCOREBOARD_REFRESH_TOKEN": FIXTURE_TOKEN},
        )
        self.assertEqual(redirect_run.returncode, 1)
        self.assertEqual([item["path"] for item in redirected.requests], ["/api/refresh-sales"])
        self.assertIn("redirect", redirect_run.stderr)

        slow = LoopbackServer(
            [
                {
                    "status": 200,
                    "content_type": "application/json",
                    "body": b'{"ok": true}',
                    "delay": 2,
                }
            ]
        )
        self.addCleanup(slow.close)
        timed = self.run_cli(
            [
                "--profile",
                "sales",
                "--path",
                "/api/refresh-sales",
                "--token-env",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                slow.base_url,
                "--timeout-seconds",
                "1",
                "--max-attempts",
                "1",
                "--backoff-seconds",
                "0",
            ],
            {"SCOREBOARD_REFRESH_TOKEN": FIXTURE_TOKEN},
        )
        self.assertEqual(timed.returncode, 1)
        self.assertIn("timed out", timed.stderr)

    def test_kpi_skip_fallback_token_and_closed_port(self):
        server = LoopbackServer([])
        self.addCleanup(server.close)
        skipped = self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "   ",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--token-env-fallback",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                server.base_url,
                "--event",
                "schedule",
                "--require-chicago-hour",
                "6",
                "--test-hooks",
                "--now",
                "2026-07-15T11:05:00Z",
            ]
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertEqual(server.requests, [])
        self.assertIn("KPI_REFRESH_PATH is unset", skipped.stdout)
        self.assertIn("::notice::", self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--base-url",
                server.base_url,
            ],
            {"GITHUB_ACTIONS": "true"},
        ).stdout)

        hour_skip = self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "/api/refresh-kpis",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--base-url",
                server.base_url,
                "--event",
                "schedule",
                "--require-chicago-hour",
                "6",
                "--test-hooks",
                "--now",
                "2026-01-15T11:05:00Z",
            ]
        )
        self.assertEqual(hour_skip.returncode, 0, hour_skip.stderr)
        self.assertEqual(server.requests, [])
        self.assertIn("05:05", hour_skip.stdout)

        body = ExecuteTests._kpi_body()
        authed = LoopbackServer(
            [{"status": 200, "content_type": "application/json", "body": body}]
        )
        self.addCleanup(authed.close)
        fallback = self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "/api/refresh-kpis/",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--token-env-fallback",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                authed.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "1",
            ],
            {"SCOREBOARD_REFRESH_TOKEN": "fallback-fixture-token"},
        )
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertEqual(authed.requests[0]["path"], "/api/refresh-kpis")
        self.assertEqual(authed.requests[0]["auth"], "Bearer fallback-fixture-token")
        self.assertIn("Token source: SCOREBOARD_REFRESH_TOKEN", fallback.stdout)
        self.assertNotIn("fallback-fixture-token", fallback.stdout + fallback.stderr)

        missing = self.run_cli(
            [
                "--profile",
                "sales",
                "--path",
                "/api/refresh-sales",
                "--token-env",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                server.base_url,
            ]
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("SCOREBOARD_REFRESH_TOKEN", missing.stderr)
        self.assertIn("unset", missing.stderr)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        refused = self.run_cli(
            [
                "--profile",
                "sales",
                "--path",
                "/api/refresh-sales",
                "--token-env",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                f"http://127.0.0.1:{closed_port}",
                "--timeout-seconds",
                "2",
                "--max-attempts",
                "2",
                "--backoff-seconds",
                "0",
            ],
            {"SCOREBOARD_REFRESH_TOKEN": FIXTURE_TOKEN},
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Attempt 2/2", refused.stdout)
        self.assertIn("network error", refused.stdout + refused.stderr)

    def test_proxy_environment_cannot_redirect_the_refresh(self):
        server = LoopbackServer(
            [
                {
                    "status": 200,
                    "content_type": "application/json",
                    "body": b'{"ok": true, "businessDate": "2026-11-09"}',
                }
            ]
        )
        self.addCleanup(server.close)
        completed = self.run_cli(
            [
                "--profile",
                "sales",
                "--path",
                "/api/refresh-sales",
                "--token-env",
                "SCOREBOARD_REFRESH_TOKEN",
                "--base-url",
                server.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "1",
            ],
            {
                "SCOREBOARD_REFRESH_TOKEN": FIXTURE_TOKEN,
                "http_proxy": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9",
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["auth"], f"Bearer {FIXTURE_TOKEN}")

    def test_blank_dispatch_path_uses_the_fallback_path(self):
        body = ExecuteTests._kpi_body()
        server = LoopbackServer(
            [{"status": 200, "content_type": "application/json", "body": body}]
        )
        self.addCleanup(server.close)
        completed = self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "   ",
                "--path-fallback",
                "/api/refresh-kpis",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--base-url",
                server.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "1",
                "--event",
                "workflow_dispatch",
            ],
            {"SCOREBOARD_KPI_REFRESH_TOKEN": FIXTURE_TOKEN},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(server.requests[0]["path"], "/api/refresh-kpis")
        self.assertIn("Token source: SCOREBOARD_KPI_REFRESH_TOKEN", completed.stdout)

    def test_now_requires_the_test_hook(self):
        completed = self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--base-url",
                "http://127.0.0.1:9",
                "--now",
                "2026-01-15T12:05:00Z",
            ]
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("--test-hooks", completed.stderr)


class WorkflowContractTests(unittest.TestCase):
    def test_workflows_and_readme_match_the_scheduler_contract(self):
        sales = (ROOT / ".github/workflows/refresh-scoreboard.yml").read_text()
        kpi = (ROOT / ".github/workflows/refresh-kpis.yml").read_text()
        tests = (ROOT / ".github/workflows/test-refresh-call.yml").read_text()
        readme = (ROOT / "README.md").read_text()
        checkout = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"

        self.assertIn('cron: "*/5 * * * *"', sales)
        self.assertIn("workflow_dispatch:", sales)
        self.assertIn("group: scoreboard-sales-refresh", sales)
        self.assertIn("cancel-in-progress: false", sales)
        self.assertIn("timeout-minutes: 6", sales)
        self.assertIn("SCOREBOARD_REFRESH_TOKEN", sales)
        self.assertIn("--profile sales", sales)
        self.assertIn("--path /api/refresh-sales", sales)
        self.assertIn("--timeout-seconds 60", sales)
        self.assertIn("--max-attempts 3", sales)
        self.assertIn("--backoff-seconds 10,30", sales)
        self.assertIn("--allow-remote", sales)
        self.assertIn(checkout, sales)
        self.assertIn("persist-credentials: false", sales)
        self.assertNotIn("--test-hooks", sales)
        self.assertNotIn("--base-url", sales)
        self.assertNotIn("KPI_REFRESH_PATH", sales)
        self.assertNotIn("hunger", sales.lower())

        self.assertIn('cron: "5 11 * * *"', kpi)
        self.assertIn('cron: "5 12 * * *"', kpi)
        self.assertIn("workflow_dispatch:", kpi)
        self.assertIn("kpi_path:", kpi)
        self.assertIn("group: scoreboard-kpi-refresh", kpi)
        self.assertIn("cancel-in-progress: false", kpi)
        self.assertIn("timeout-minutes: 10", kpi)
        self.assertIn("SCOREBOARD_KPI_REFRESH_TOKEN", kpi)
        self.assertIn("SCOREBOARD_REFRESH_TOKEN", kpi)
        self.assertIn("KPI_REFRESH_PATH", kpi)
        self.assertIn("--profile kpi", kpi)
        self.assertIn("--path-fallback", kpi)
        self.assertIn("KPI_PATH_VAR", kpi)
        self.assertIn("--token-env SCOREBOARD_KPI_REFRESH_TOKEN", kpi)
        self.assertIn("--token-env-fallback SCOREBOARD_REFRESH_TOKEN", kpi)
        self.assertIn("--require-chicago-hour 6", kpi)
        self.assertIn("--timeout-seconds 120", kpi)
        self.assertIn("--allow-remote", kpi)
        self.assertIn(checkout, kpi)
        self.assertIn("persist-credentials: false", kpi)
        self.assertNotIn("--test-hooks", kpi)
        self.assertNotIn("--base-url", kpi)
        self.assertNotIn("hunger", kpi.lower())

        self.assertIn("python3 scripts/test_refresh_call.py", tests)
        self.assertNotIn("secrets.", tests)
        self.assertNotIn("--allow-remote", tests)
        self.assertNotIn(PRODUCTION_HOST, tests)

        for needle in (
            "SCOREBOARD_REFRESH_TOKEN",
            "SCOREBOARD_KPI_REFRESH_TOKEN",
            "KPI_REFRESH_PATH",
            "/api/refresh-kpis",
            "/api/refresh-sales",
            "POST",
            "Bearer",
            "application/json",
            "ok",
            "builtAt",
            "weeksUpdated",
            "lastSuccess",
            "coverage",
            "stale",
            "lastError",
            "America/Chicago",
            "6:05",
            "HTML",
            "python3 scripts/test_refresh_call.py",
            PRODUCTION_HOST,
        ):
            self.assertIn(needle, readme)

        self.assertNotIn(FIXTURE_TOKEN, readme + sales + kpi + tests)
        script = (ROOT / "scripts" / "refresh_call.py").read_text()
        self.assertNotIn(FIXTURE_TOKEN, script)
        self.assertNotIn("hungerrush", script.lower())
        self.assertNotIn("hunger_rush", script.lower())


if __name__ == "__main__":
    unittest.main()
