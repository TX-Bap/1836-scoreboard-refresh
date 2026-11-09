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
        "require_chicago_slots": (),
        "chicago_slot_tolerance_minutes": 40,
        "schedule": "",
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
        self.assertIn("require_chicago_hour", dests)
        self.assertIn("require_chicago_slots", dests)
        self.assertIn("chicago_slot_tolerance_minutes", dests)
        self.assertIn("schedule", dests)
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

    def test_chicago_slot_cli_covers_skip_dispatch_and_bad_input(self):
        idle = LoopbackServer([])
        self.addCleanup(idle.close)
        slot_args = [
            "--profile",
            "kpi",
            "--token-env",
            "SCOREBOARD_KPI_REFRESH_TOKEN",
            "--base-url",
            idle.base_url,
            "--require-chicago-slots",
            KPI_SLOTS,
            "--chicago-slot-tolerance-minutes",
            "40",
            "--test-hooks",
        ]
        tuesday = self.run_cli(
            [
                *slot_args,
                "--path",
                "/api/refresh-kpis",
                "--event",
                "schedule",
                "--schedule",
                "15 6 * * 1",
                "--now",
                "2026-01-13T06:15:00Z",
            ]
        )
        self.assertEqual(tuesday.returncode, 0, tuesday.stderr)
        self.assertEqual(idle.requests, [])
        self.assertIn("2026-01-13 00:15", tuesday.stdout)
        self.assertIn("No request was sent", tuesday.stdout)
        self.assertIn("::notice::", self.run_cli(
            [
                *slot_args,
                "--path",
                "/api/refresh-kpis",
                "--event",
                "schedule",
                "--schedule",
                "0 10 * * 1",
                "--now",
                "2026-01-12T10:00:00Z",
            ],
            {"GITHUB_ACTIONS": "true"},
        ).stdout)

        summer = LoopbackServer(
            [{"status": 200, "content_type": "application/json", "body": ExecuteTests._kpi_body()}]
        )
        self.addCleanup(summer.close)
        matched = self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "/api/refresh-kpis",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--base-url",
                summer.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "1",
                "--event",
                "schedule",
                "--require-chicago-slots",
                KPI_SLOTS,
                "--schedule",
                "15 5 * * 1",
                "--test-hooks",
                "--now",
                "2026-07-13T05:15:00Z",
            ],
            {"SCOREBOARD_KPI_REFRESH_TOKEN": FIXTURE_TOKEN},
        )
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertEqual(len(summer.requests), 1)
        self.assertIn("KPI refresh succeeded", matched.stdout)

        dispatch = LoopbackServer(
            [{"status": 200, "content_type": "application/json", "body": ExecuteTests._kpi_body()}]
        )
        self.addCleanup(dispatch.close)
        manual = self.run_cli(
            [
                "--profile",
                "kpi",
                "--path",
                "/api/refresh-kpis",
                "--token-env",
                "SCOREBOARD_KPI_REFRESH_TOKEN",
                "--base-url",
                dispatch.base_url,
                "--timeout-seconds",
                "5",
                "--max-attempts",
                "1",
                "--event",
                "workflow_dispatch",
                "--require-chicago-slots",
                KPI_SLOTS,
                "--schedule",
                "15 6 * * 1",
                "--test-hooks",
                "--now",
                "2026-01-13T18:00:00Z",
            ],
            {"SCOREBOARD_KPI_REFRESH_TOKEN": FIXTURE_TOKEN},
        )
        self.assertEqual(manual.returncode, 0, manual.stderr)
        self.assertEqual(len(dispatch.requests), 1)

        rejected = self.run_cli(
            [
                *slot_args,
                "--path",
                "",
                "--event",
                "schedule",
                "--require-chicago-slots",
                "Mon-25:00",
                "--now",
                "2026-01-12T06:15:00Z",
            ]
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("Chicago slot", rejected.stderr)
        self.assertEqual(idle.requests, [])

        bad_cron = self.run_cli(
            [
                *slot_args,
                "--path",
                "",
                "--event",
                "schedule",
                "--schedule",
                "not-a-cron",
                "--now",
                "2026-01-12T06:15:00Z",
            ]
        )
        self.assertEqual(bad_cron.returncode, 1)
        self.assertIn("5-field cron", bad_cron.stderr)

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


KPI_SLOTS = "Mon-00:15,Mon-04:15,Mon-05:00"


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class ScheduleGuardTests(unittest.TestCase):
    def _run_guard(self, stamp, schedule, *, event="schedule", tolerance=40, hour=None, path="/api/refresh-kpis", token=None, slots=KPI_SLOTS):
        expect_call = token is not None
        transport = RecordingTransport([(200, "application/json", ExecuteTests._kpi_body())])

        def boom(url, token_value, timeout):
            raise AssertionError(f"unexpected call to {url}")

        outcome = refresh_call.execute(
            make_config(
                profile="kpi",
                path=path,
                token="" if token is None else token,
                event=event,
                require_chicago_hour=hour,
                require_chicago_slots=refresh_call.parse_chicago_slots(slots),
                chicago_slot_tolerance_minutes=tolerance,
                schedule=schedule,
                now=_utc(stamp),
            ),
            transport if expect_call else boom,
            lambda delay: None,
        )
        return outcome, transport

    def test_dst_monday_slots_and_tuesday_and_tolerance(self):
        cases = (
            # Winter CST Monday: only the UTC-6 registrations match.
            ("2026-01-12T06:15:00Z", "15 6 * * 1", "success", ""),
            ("2026-01-12T05:15:00Z", "15 5 * * 1", "skipped", "outside allowed slots"),
            ("2026-01-12T10:15:00Z", "15 10 * * 1", "success", ""),
            ("2026-01-12T09:15:00Z", "15 9 * * 1", "skipped", "outside allowed slots"),
            ("2026-01-12T11:00:00Z", "0 11 * * 1", "success", ""),
            # CDT 5:00 cron during CST is 04:00, 15 minutes from the 4:15 slot.
            ("2026-01-12T10:00:00Z", "0 10 * * 1", "skipped", "outside allowed slots"),
            # Summer CDT Monday: only the UTC-5 registrations match.
            ("2026-07-13T05:15:00Z", "15 5 * * 1", "success", ""),
            ("2026-07-13T06:15:00Z", "15 6 * * 1", "skipped", "outside allowed slots"),
            ("2026-07-13T09:15:00Z", "15 9 * * 1", "success", ""),
            # CST 4:15 cron during CDT is 05:15, 15 minutes from the 5:00 slot.
            ("2026-07-13T10:15:00Z", "15 10 * * 1", "skipped", "outside allowed slots"),
            ("2026-07-13T10:00:00Z", "0 10 * * 1", "success", ""),
            ("2026-07-13T11:00:00Z", "0 11 * * 1", "skipped", "outside allowed slots"),
            # Monday after the March and November transitions.
            ("2026-03-09T05:15:00Z", "15 5 * * 1", "success", ""),
            ("2026-03-09T06:15:00Z", "15 6 * * 1", "skipped", "outside allowed slots"),
            ("2026-03-09T10:00:00Z", "0 10 * * 1", "success", ""),
            ("2026-11-02T06:15:00Z", "15 6 * * 1", "success", ""),
            ("2026-11-02T05:15:00Z", "15 5 * * 1", "skipped", "outside allowed slots"),
            ("2026-11-02T11:00:00Z", "0 11 * * 1", "success", ""),
            ("2026-11-02T10:00:00Z", "0 10 * * 1", "skipped", "outside allowed slots"),
            # Tuesday does not match a Monday cron, in either offset.
            ("2026-01-13T06:15:00Z", "15 6 * * 1", "skipped", "2026-01-13 00:15"),
            ("2026-07-14T05:15:00Z", "15 5 * * 1", "skipped", "2026-07-14 00:15"),
            # ±40 minutes from the triggering cron, including the early side.
            ("2026-07-13T10:40:00Z", "0 10 * * 1", "success", ""),
            ("2026-07-13T10:41:00Z", "0 10 * * 1", "skipped", "41 minutes"),
            ("2026-07-13T09:20:00Z", "0 10 * * 1", "success", ""),
            ("2026-07-13T09:19:00Z", "0 10 * * 1", "skipped", "41 minutes"),
            ("2026-01-12T06:55:00Z", "15 6 * * 1", "success", ""),
            ("2026-01-12T06:56:00Z", "15 6 * * 1", "skipped", "41 minutes"),
            # Without a cron, local Chicago time matches a slot inside ±40 minutes.
            ("2026-01-12T06:15:00Z", "", "success", ""),
            ("2026-07-13T05:15:00Z", "", "success", ""),
            ("2026-01-12T05:15:00Z", "", "skipped", "outside allowed slots"),
            ("2026-07-13T06:15:00Z", "", "skipped", "outside allowed slots"),
            ("2026-01-13T06:15:00Z", "", "skipped", "2026-01-13 00:15"),
            ("2026-01-12T08:30:00Z", "", "skipped", "outside allowed slots"),
            ("2026-01-12T05:35:00Z", "", "success", ""),
            ("2026-01-12T05:34:00Z", "", "skipped", "outside allowed slots"),
            ("2026-01-12T06:55:00Z", "", "success", ""),
            ("2026-01-12T06:56:00Z", "", "skipped", "outside allowed slots"),
            ("2026-07-13T04:35:00Z", "", "success", ""),
            ("2026-07-13T04:34:00Z", "", "skipped", "outside allowed slots"),
            # Local window alone accepts the neighboring DST registration.
            ("2026-07-13T10:15:00Z", "", "success", ""),
            ("2026-01-12T10:00:00Z", "", "success", ""),
        )
        for stamp, schedule, expected, needle in cases:
            with self.subTest(stamp=stamp, schedule=schedule or "(local)", expected=expected):
                token = FIXTURE_TOKEN if expected == "success" else None
                outcome, transport = self._run_guard(stamp, schedule, token=token)
                self.assertEqual(outcome.kind, expected)
                if expected == "success":
                    self.assertEqual(len(transport.calls), 1)
                    self.assertIn("KPI refresh succeeded", outcome.message)
                    assert_no_token(self, outcome.message)
                else:
                    self.assertEqual(outcome.attempts, 0)
                    self.assertIn("No request was sent", outcome.message)
                    self.assertIn("America/Chicago", outcome.message)
                    if needle:
                        self.assertIn(needle, outcome.message)
                    assert_no_token(self, outcome.message)

    def test_workflow_dispatch_bypasses_slots_and_a_misleading_cron(self):
        outcome, transport = self._run_guard(
            "2026-01-13T18:00:00Z",
            "15 6 * * 1",
            event="workflow_dispatch",
            token=FIXTURE_TOKEN,
        )
        self.assertEqual(outcome.kind, "success")
        self.assertEqual(len(transport.calls), 1)

    def test_matching_slot_with_an_empty_path_skips_before_the_request(self):
        outcome, _transport = self._run_guard(
            "2026-01-12T06:15:00Z",
            "15 6 * * 1",
            path="  ",
            token="",
        )
        self.assertEqual(outcome.kind, "skipped")
        self.assertIn("KPI_REFRESH_PATH is unset", outcome.message)
        self.assertIn("No request was sent", outcome.message)

    def test_hour_gate_still_skips_and_still_allows_the_matching_hour(self):
        skipped, _transport = self._run_guard(
            "2026-01-15T11:05:00Z",
            "",
            hour=6,
            slots="",
            token=None,
        )
        self.assertEqual(skipped.kind, "skipped")
        self.assertIn("05:05", skipped.message)
        self.assertIn("06:00 hour", skipped.message)

        allowed, transport = self._run_guard(
            "2026-01-15T12:05:00Z",
            "",
            hour=6,
            slots="",
            token=FIXTURE_TOKEN,
        )
        self.assertEqual(allowed.kind, "success")
        self.assertEqual(len(transport.calls), 1)

        manual, manual_transport = self._run_guard(
            "2026-01-15T11:05:00Z",
            "",
            event="workflow_dispatch",
            hour=6,
            slots="",
            token=FIXTURE_TOKEN,
        )
        self.assertEqual(manual.kind, "success")
        self.assertEqual(len(manual_transport.calls), 1)

    def test_sales_schedule_without_a_guard_still_calls(self):
        transport = RecordingTransport([(200, "application/json", b'{"ok": true}')])
        outcome = refresh_call.execute(
            make_config(
                event="schedule",
                now=_utc("2026-01-13T18:00:00Z"),
            ),
            transport,
            lambda delay: None,
        )
        self.assertEqual(outcome.kind, "success")
        self.assertEqual(len(transport.calls), 1)

    def test_parser_rejects_bad_slots_crons_and_tolerance(self):
        parsed = refresh_call.parse_chicago_slots("Mon-00:15, mon-04:15,Mon-00:15,")
        self.assertEqual([slot.label() for slot in parsed], ["Mon-00:15", "Mon-04:15"])
        self.assertEqual(refresh_call.parse_chicago_slots("  "), ())
        sunday = refresh_call.parse_cron_schedule("0 11 * * 7")
        self.assertEqual(sunday.dow, 0)
        monday = refresh_call.parse_cron_schedule("15 5 * * 1")
        self.assertEqual((monday.minute, monday.hour, monday.dow), (15, 5, 1))
        for text in ("Mon-24:00", "Monday-00:15", "Mon-00:15,Foo-01:00", "00:15", "Mon-4:15"):
            with self.subTest(text=text):
                with self.assertRaises(refresh_call.RefreshError):
                    refresh_call.parse_chicago_slots(text)
        for text in ("15 5 * *", "15 * * * 1", "15 5 * * 8"):
            with self.subTest(text=text):
                with self.assertRaises(refresh_call.RefreshError):
                    refresh_call.parse_cron_schedule(text)


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
        self.assertNotIn("--require-chicago-slots", sales)
        self.assertNotIn("--require-chicago-hour", sales)
        self.assertNotIn("hunger", sales.lower())

        monday_crons = (
            "15 5 * * 1",
            "15 6 * * 1",
            "15 9 * * 1",
            "15 10 * * 1",
            "0 10 * * 1",
            "0 11 * * 1",
        )
        for cron in monday_crons:
            self.assertIn(f'cron: "{cron}"', kpi)
            self.assertIn(f"`{cron}`", readme)
        self.assertNotIn('cron: "5 11 * * *"', kpi)
        self.assertNotIn('cron: "5 12 * * *"', kpi)
        self.assertNotIn("6:05", kpi)
        self.assertNotIn("6:05", readme)
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
        self.assertIn("--require-chicago-slots Mon-00:15,Mon-04:15,Mon-05:00", kpi)
        self.assertIn("--chicago-slot-tolerance-minutes 40", kpi)
        self.assertIn("--schedule", kpi)
        self.assertIn("KPI_SCHEDULE", kpi)
        self.assertNotIn("--require-chicago-hour", kpi)
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
            "12:15 a.m.",
            "4:15 a.m.",
            "5:00 a.m.",
            "Mon-00:15,Mon-04:15,Mon-05:00",
            "±40",
            "Monday",
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
