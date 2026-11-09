#!/usr/bin/env python3
"""Call one 1836 Scoreboard refresh endpoint and decide success from the response.

The bearer token is read from an environment variable named on the command line.
This process prints the variable name and never prints the token value.

Sales success is HTTP 200, a JSON content type, and a boolean ok value of true.
An HTML document with HTTP 200 is a failure and is not retried. Network errors,
timeouts, and HTTP 408, 429, 500, 502, 503, and 504 are retried with backoff.
The KPI profile adds the snapshot fields documented in the repository README.
An empty KPI path skips the run so the Monday schedule can stay on before the
app endpoint exists. Scheduled runs can be limited to a Chicago hour, or to
weekday and minute slots, so a daylight-saving pair of crons does not both
call the app. workflow_dispatch ignores that guard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

DEFAULT_BASE_URL = "https://lagoon-brook-orchid-pearl.grok.me"
ALLOWED_REMOTE_HOST = "lagoon-brook-orchid-pearl.grok.me"
DEFAULT_KPI_PATH = "/api/refresh-kpis"
TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "1836-scoreboard-refresh/1.0"
CHICAGO = ZoneInfo("America/Chicago")
DEFAULT_SLOT_TOLERANCE_MINUTES = 40
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_INDEX = {label.lower(): index for index, label in enumerate(_WEEKDAY_LABELS)}
MAX_BODY_BYTES = 1_000_000
EXCERPT_CHARS = 400
PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{1,200}$")
SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SALES_SUMMARY_KEYS = (
    "run",
    "businessDate",
    "updatedAt",
    "toastCollectedAt",
    "hungerCollectedAt",
    "hungerIncluded",
    "hungerRule",
    "lastError",
)

Transport = Callable[[str, str, float], tuple[int, str, bytes]]
Sleeper = Callable[[float], None]


class RefreshError(Exception):
    """A refresh attempt failed. retry is true only for transient failures."""

    def __init__(
        self,
        reason: str,
        *,
        retry: bool,
        status: Optional[int] = None,
        excerpt: str = "",
        content_type: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry = retry
        self.status = status
        self.excerpt = excerpt
        self.content_type = content_type


@dataclass
class Outcome:
    kind: str
    message: str
    logs: list[str]
    attempts: int = 0


@dataclass(frozen=True)
class ChicagoSlot:
    """America/Chicago weekday and clock time. weekday uses datetime.weekday() (Monday is 0)."""

    weekday: int
    hour: int
    minute: int

    def label(self) -> str:
        return f"{_WEEKDAY_LABELS[self.weekday]}-{self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True)
class CronSchedule:
    """Five-field cron. dow uses GitHub numbering: Sunday is 0, Monday is 1. None means *."""

    minute: int
    hour: int
    dom: Optional[int]
    month: Optional[int]
    dow: Optional[int]


@dataclass
class Config:
    profile: str
    path: str
    token: str
    token_source: str
    base_url: str
    timeout: float
    max_attempts: int
    backoff: tuple[float, ...]
    event: str
    require_chicago_hour: Optional[int] = None
    require_chicago_slots: tuple[ChicagoSlot, ...] = ()
    chicago_slot_tolerance_minutes: int = DEFAULT_SLOT_TOLERANCE_MINUTES
    schedule: str = ""
    now: Optional[datetime] = None


def parse_backoff(text: str) -> tuple[float, ...]:
    parts = [part.strip() for part in text.split(",") if part.strip() != ""]
    if not parts:
        raise argparse.ArgumentTypeError("backoff schedule is empty")
    values: list[float] = []
    for part in parts:
        if not re.fullmatch(r"\d+(\.\d+)?", part):
            raise argparse.ArgumentTypeError(
                f"backoff value must be a non-negative number of seconds: {part}"
            )
        value = float(part)
        if value > 300:
            raise argparse.ArgumentTypeError("backoff value must be 300 seconds or less")
        values.append(value)
    return tuple(values)


def annotation_safe(text: str) -> str:
    return (
        text.replace("%", "%25")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("::", " ")
    )


def redact(text: str, secrets: Sequence[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(
        r"(?i)(authorization|token|secret|password)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        redacted,
    )
    return redacted


def excerpt_body(body: bytes, secrets: Sequence[str]) -> str:
    text = body[:8000].decode("utf-8", errors="replace")
    text = redact(text, secrets)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > EXCERPT_CHARS:
        return text[:EXCERPT_CHARS] + "..."
    return text


def display_path(path: str) -> str:
    if path == "":
        return "(empty)"
    if PATH_RE.fullmatch(path) and ".." not in path and "//" not in path:
        return path
    return "<rejected-path>"


def content_type_is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json"


def looks_like_html(content_type: str, body: bytes) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    if media in {"text/html", "application/xhtml+xml"}:
        return True
    sample = body.lstrip()[:200].lower()
    return (
        sample.startswith(b"<!doctype html")
        or sample.startswith(b"<html")
        or sample.startswith(b"<head")
        or sample.startswith(b"<body")
    )


def parse_iso8601(value: object, field: str) -> None:
    if not isinstance(value, str) or value.strip() == "":
        raise RefreshError(f"{field} must be an ISO-8601 string", retry=False)
    text = value.strip()
    if "T" not in text:
        raise RefreshError(f"{field} must be an ISO-8601 string", retry=False)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise RefreshError(f"{field} must be an ISO-8601 string", retry=False) from exc


def safe_scalar(value: object, secrets: Sequence[str]) -> str:
    if value is None:
        rendered = "null"
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    elif isinstance(value, str):
        rendered = re.sub(r"\s+", " ", value).strip()
        if len(rendered) > 120:
            rendered = rendered[:120] + "..."
    else:
        rendered = "<unsupported>"
    return redact(rendered, secrets)


def summarize_sales(payload: dict, secrets: Sequence[str]) -> str:
    parts = []
    for key in SALES_SUMMARY_KEYS:
        if key in payload:
            parts.append(f"{key}={safe_scalar(payload[key], secrets)}")
    if not parts:
        return "ok=true"
    return " ".join(parts)


def summarize_kpi(payload: dict, secrets: Sequence[str]) -> str:
    lines = [
        f"builtAt={safe_scalar(payload['builtAt'], secrets)}",
        f"weeksUpdated={safe_scalar(payload['weeksUpdated'], secrets)}",
        f"lastError={safe_scalar(payload['lastError'], secrets)}",
    ]
    sources = payload["sources"]
    if isinstance(sources, dict):
        for name, source in sources.items():
            shown = name if isinstance(name, str) and SOURCE_NAME_RE.fullmatch(name) else "<redacted-name>"
            if not isinstance(source, dict):
                continue
            lines.append(
                "source {name}: lastSuccess={last} coverage={coverage} stale={stale}".format(
                    name=shown,
                    last=safe_scalar(source.get("lastSuccess"), secrets),
                    coverage=safe_scalar(source.get("coverage"), secrets),
                    stale=safe_scalar(source.get("stale"), secrets),
                )
            )
    return "\n".join(lines)


def validate_sales(payload: object, secrets: Sequence[str]) -> str:
    if not isinstance(payload, dict):
        raise RefreshError("response JSON must be an object", retry=False)
    if payload.get("ok") is not True:
        raise RefreshError("response JSON ok must be boolean true", retry=False)
    return summarize_sales(payload, secrets)


def validate_kpi(payload: object, secrets: Sequence[str]) -> str:
    if not isinstance(payload, dict):
        raise RefreshError("response JSON must be an object", retry=False)
    if payload.get("ok") is not True:
        raise RefreshError("response JSON ok must be boolean true", retry=False)
    for key in ("builtAt", "weeksUpdated", "sources", "lastError"):
        if key not in payload:
            raise RefreshError(f"response JSON is missing {key}", retry=False)
    parse_iso8601(payload["builtAt"], "builtAt")
    weeks = payload["weeksUpdated"]
    if isinstance(weeks, bool) or not isinstance(weeks, int) or weeks < 0:
        raise RefreshError(
            "weeksUpdated must be an integer greater than or equal to 0",
            retry=False,
        )
    sources = payload["sources"]
    if not isinstance(sources, dict):
        raise RefreshError("sources must be an object", retry=False)
    for name, source in sources.items():
        label = name if isinstance(name, str) and SOURCE_NAME_RE.fullmatch(name) else "<redacted-name>"
        if not isinstance(source, dict):
            raise RefreshError(f"sources[{label}] must be an object", retry=False)
        for required in ("lastSuccess", "coverage", "stale"):
            if required not in source:
                raise RefreshError(f"sources[{label}] is missing {required}", retry=False)
        if source["lastSuccess"] is not None:
            parse_iso8601(source["lastSuccess"], f"sources[{label}].lastSuccess")
        coverage = source["coverage"]
        if coverage is not None and not isinstance(coverage, str):
            raise RefreshError(
                f"sources[{label}].coverage must be a string or null",
                retry=False,
            )
        if not isinstance(source["stale"], bool):
            raise RefreshError(f"sources[{label}].stale must be a boolean", retry=False)
    if payload["lastError"] is not None and not isinstance(payload["lastError"], str):
        raise RefreshError("lastError must be a string or null", retry=False)
    return summarize_kpi(payload, secrets)


def validate_base_url(url: str, allow_remote: bool) -> str:
    raw = url.strip()
    if raw == "":
        raise RefreshError("base URL is empty", retry=False)
    parts = urlsplit(raw)
    if parts.username or parts.password:
        raise RefreshError("base URL must not include credentials", retry=False)
    if parts.query or parts.fragment:
        raise RefreshError("base URL must not include a query string or fragment", retry=False)
    if parts.path not in ("", "/"):
        raise RefreshError("base URL must not include a path; pass the path separately", retry=False)
    host = (parts.hostname or "").lower()
    if host == "":
        raise RefreshError("base URL is missing a host", retry=False)
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parts.scheme == "http":
        if not loopback:
            raise RefreshError("plain HTTP is only allowed for a loopback host", retry=False)
    elif parts.scheme == "https":
        if not allow_remote:
            raise RefreshError("refusing to call a remote host without --allow-remote", retry=False)
        if host != ALLOWED_REMOTE_HOST:
            raise RefreshError(
                f"remote host must be {ALLOWED_REMOTE_HOST}",
                retry=False,
            )
    else:
        raise RefreshError("base URL must use https, or http on loopback", retry=False)
    netloc = parts.netloc
    if "@" in netloc:
        raise RefreshError("base URL must not include credentials", retry=False)
    return urlunsplit((parts.scheme, netloc, "", "", "")).rstrip("/")


def normalize_path(path: str, profile: str) -> str:
    cleaned = path.strip()
    if cleaned == "":
        if profile == "kpi":
            return ""
        raise RefreshError("sales refresh path is empty", retry=False)
    if (
        "\\" in cleaned
        or ".." in cleaned
        or "//" in cleaned
        or "@" in cleaned
        or "?" in cleaned
        or "#" in cleaned
        or cleaned.startswith("//")
    ):
        raise RefreshError(
            f"{profile} refresh path was rejected: {display_path(cleaned)}",
            retry=False,
        )
    if not PATH_RE.fullmatch(cleaned):
        raise RefreshError(
            f"{profile} refresh path was rejected: {display_path(cleaned)}",
            retry=False,
        )
    if cleaned != "/" and cleaned.endswith("/"):
        cleaned = cleaned.rstrip("/")
    return cleaned


def resolve_token(primary_name: str, fallback_name: str) -> tuple[str, str]:
    def read(name: str) -> str:
        if name == "":
            return ""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RefreshError("token environment variable name is invalid", retry=False)
        value = os.environ.get(name, "")
        if value == "":
            return ""
        if any(char in value for char in "\r\n\x00"):
            raise RefreshError(
                f"secret {name} contains a newline or null and will not be sent",
                retry=False,
            )
        return value

    primary = read(primary_name)
    if primary != "":
        return primary, primary_name
    fallback = read(fallback_name)
    if fallback != "":
        return fallback, fallback_name
    if fallback_name:
        names = f"{primary_name} or {fallback_name}"
    else:
        names = primary_name
    raise RefreshError(
        f"secret {names} is unset. Add the bearer token in GitHub Actions secrets. "
        "The token value is not printed.",
        retry=False,
    )


def chicago_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(CHICAGO)
    if now.tzinfo is None:
        raise RefreshError("schedule clock must include a timezone", retry=False)
    return now.astimezone(CHICAGO)


def parse_chicago_slots(text: str) -> tuple[ChicagoSlot, ...]:
    raw = text.strip()
    if raw == "":
        return ()
    slots: list[ChicagoSlot] = []
    seen: set[ChicagoSlot] = set()
    for part in raw.split(","):
        piece = part.strip()
        if piece == "":
            continue
        shown = piece if len(piece) <= 40 else piece[:40] + "..."
        match = re.fullmatch(r"([A-Za-z]{3})-(\d{2}):(\d{2})", piece)
        if match is None:
            raise RefreshError(
                f"Chicago slot must look like Mon-00:15: {shown}",
                retry=False,
            )
        weekday = _WEEKDAY_INDEX.get(match.group(1).lower())
        hour = int(match.group(2))
        minute = int(match.group(3))
        if weekday is None or hour > 23 or minute > 59:
            raise RefreshError(
                f"Chicago slot is not a real local time: {shown}",
                retry=False,
            )
        slot = ChicagoSlot(weekday, hour, minute)
        if slot not in seen:
            seen.add(slot)
            slots.append(slot)
    if not slots:
        raise RefreshError("Chicago slot list is empty", retry=False)
    if len(slots) > 48:
        raise RefreshError("at most 48 Chicago slots are allowed", retry=False)
    return tuple(slots)


def _cron_number(field: str, low: int, high: int, name: str) -> int:
    if not re.fullmatch(r"\d+", field):
        raise RefreshError(f"schedule {name} must be a number from {low} to {high}", retry=False)
    value = int(field)
    if value < low or value > high:
        raise RefreshError(f"schedule {name} must be a number from {low} to {high}", retry=False)
    return value


def parse_cron_schedule(text: str) -> CronSchedule:
    fields = text.split()
    if len(fields) != 5:
        raise RefreshError(
            "schedule must be a 5-field cron such as 15 5 * * 1",
            retry=False,
        )
    minute = _cron_number(fields[0], 0, 59, "minute")
    hour = _cron_number(fields[1], 0, 23, "hour")
    dom = None if fields[2] == "*" else _cron_number(fields[2], 1, 31, "day of month")
    month = None if fields[3] == "*" else _cron_number(fields[3], 1, 12, "month")
    if fields[4] == "*":
        dow = None
    else:
        dow = _cron_number(fields[4], 0, 7, "day of week")
        if dow == 7:
            dow = 0
    return CronSchedule(minute, hour, dom, month, dow)


def format_slots(slots: Sequence[ChicagoSlot]) -> str:
    ordered = sorted(slots, key=lambda slot: (slot.weekday, slot.hour, slot.minute))
    return ", ".join(slot.label() for slot in ordered)


def chicago_stamp(moment: datetime) -> str:
    return moment.astimezone(CHICAGO).strftime("%Y-%m-%d %H:%M %Z")


def elapsed_minutes(delta: timedelta) -> int:
    seconds = abs(delta.total_seconds())
    return int((seconds + 59) // 60)


def closest_slot(
    local: datetime, slots: Sequence[ChicagoSlot]
) -> tuple[Optional[ChicagoSlot], Optional[timedelta]]:
    local = local.astimezone(CHICAGO)
    best_slot: Optional[ChicagoSlot] = None
    best_delta: Optional[timedelta] = None
    for slot in slots:
        for offset in (-1, 0, 1):
            day = local.date() + timedelta(days=offset)
            if day.weekday() != slot.weekday:
                continue
            candidate = datetime(
                day.year, day.month, day.day, slot.hour, slot.minute, tzinfo=CHICAGO
            )
            delta = abs(local - candidate)
            if best_delta is None or delta < best_delta:
                best_slot = slot
                best_delta = delta
    return best_slot, best_delta


def nearest_cron_instant(now: datetime, cron: CronSchedule) -> Optional[datetime]:
    now_utc = now.astimezone(timezone.utc)
    best: Optional[datetime] = None
    best_delta: Optional[timedelta] = None
    for offset in range(-7, 8):
        day = now_utc.date() + timedelta(days=offset)
        if cron.dom is not None and day.day != cron.dom:
            continue
        if cron.month is not None and day.month != cron.month:
            continue
        instant = datetime(
            day.year, day.month, day.day, cron.hour, cron.minute, tzinfo=timezone.utc
        )
        if cron.dow is not None and (instant.weekday() + 1) % 7 != cron.dow:
            continue
        delta = abs(now_utc - instant)
        if best_delta is None or delta < best_delta:
            best = instant
            best_delta = delta
    return best


def schedule_skip_message(
    *,
    event: str,
    now: Optional[datetime],
    require_chicago_hour: Optional[int],
    slots: Sequence[ChicagoSlot],
    tolerance_minutes: int,
    schedule: str,
) -> Optional[str]:
    """Return a skip line for a scheduled run, or None when the run should proceed.

    workflow_dispatch and any other event bypass the guard. --require-chicago-hour
    keeps the single-hour gate. --require-chicago-slots matches an America/Chicago
    weekday and clock time, within tolerance_minutes.

    When schedule is set, the cron instant itself is converted to Chicago and must
    land on an allowed slot. The run may start up to tolerance_minutes away from
    that instant. Matching the delayed local clock against every slot would
    double-fire: 4:15 and 5:00 are 45 minutes apart, and the other daylight-saving
    registration is 60 minutes off, so it falls inside a ±40 minute window of the
    neighboring slot.
    """
    if event != "schedule":
        return None
    if require_chicago_hour is None and not slots:
        return None
    local = chicago_now(now)
    stamp = chicago_stamp(local)
    if require_chicago_hour is not None and local.hour != require_chicago_hour:
        return (
            "Scheduled KPI refresh skipped: America/Chicago local time is "
            f"{stamp}, outside the {require_chicago_hour:02d}:00 hour. "
            "No request was sent."
        )
    if not slots:
        return None
    tolerance = timedelta(minutes=tolerance_minutes)
    labels = format_slots(slots)
    cron_text = " ".join(schedule.split())
    if cron_text:
        cron = parse_cron_schedule(cron_text)
        instant = nearest_cron_instant(local, cron)
        if instant is None:
            return (
                "Scheduled refresh skipped: America/Chicago local time is "
                f"{stamp}. Cron '{cron_text}' has no matching occurrence. "
                "No request was sent."
            )
        nominal = instant.astimezone(CHICAGO)
        nominal_slot = ChicagoSlot(nominal.weekday(), nominal.hour, nominal.minute)
        nominal_stamp = chicago_stamp(nominal)
        delta = abs(local.astimezone(timezone.utc) - instant)
        if nominal_slot not in set(slots):
            return (
                "Scheduled refresh skipped: cron "
                f"'{cron_text}' is {nominal_stamp} America/Chicago, outside "
                f"allowed slots {labels}. Local time is {stamp}. "
                "No request was sent."
            )
        if delta > tolerance:
            minutes = elapsed_minutes(delta)
            unit = "minute" if minutes == 1 else "minutes"
            return (
                "Scheduled refresh skipped: America/Chicago local time is "
                f"{stamp}, {minutes} {unit} from cron '{cron_text}' "
                f"({nominal_stamp}, {nominal_slot.label()}), outside the "
                f"±{tolerance_minutes} minute window for slots {labels}. "
                "No request was sent."
            )
        return None
    _slot, delta = closest_slot(local, slots)
    if delta is not None and delta <= tolerance:
        return None
    return (
        "Scheduled refresh skipped: America/Chicago local time is "
        f"{stamp}, outside allowed slots {labels} "
        f"(±{tolerance_minutes} minutes). No request was sent."
    )


def backoff_before(attempt: int, schedule: Sequence[float]) -> float:
    """Seconds to wait before attempt N (1-based). Attempt 1 waits zero."""
    if attempt <= 1:
        return 0
    index = attempt - 2
    if index >= len(schedule):
        return float(schedule[-1])
    return float(schedule[index])


def evaluate_response(
    status: int,
    content_type: str,
    body: bytes,
    profile: str,
    secrets: Sequence[str],
) -> str:
    excerpt = excerpt_body(body, secrets)
    shown_type = content_type.strip()
    if status in TRANSIENT_STATUSES:
        raise RefreshError(
            f"transient HTTP {status}",
            retry=True,
            status=status,
            excerpt=excerpt,
            content_type=shown_type,
        )
    if status in REDIRECT_STATUSES:
        raise RefreshError(
            f"HTTP {status} redirect; redirects are not followed",
            retry=False,
            status=status,
            excerpt=excerpt,
            content_type=shown_type,
        )
    if status != 200:
        if status == 401:
            reason = "HTTP 401 unauthorized"
        elif status == 403:
            reason = "HTTP 403 forbidden"
        elif status == 404:
            reason = "HTTP 404 not found"
        else:
            reason = f"HTTP {status}"
        raise RefreshError(
            reason,
            retry=False,
            status=status,
            excerpt=excerpt,
            content_type=shown_type,
        )
    if looks_like_html(content_type, body):
        raise RefreshError(
            "HTTP 200 returned HTML; an HTML page is not a successful refresh",
            retry=False,
            status=status,
            excerpt=excerpt,
            content_type=shown_type,
        )
    if not content_type_is_json(content_type):
        shown = shown_type or "(missing)"
        raise RefreshError(
            f"HTTP 200 content type must be application/json, got {shown}",
            retry=False,
            status=status,
            excerpt=excerpt,
            content_type=shown_type,
        )
    if len(body) > MAX_BODY_BYTES:
        raise RefreshError("response body exceeds 1 MB", retry=False, status=status)
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefreshError(
            "HTTP 200 body is not valid JSON",
            retry=False,
            status=status,
            excerpt=excerpt,
        ) from exc
    try:
        if profile == "sales":
            return validate_sales(payload, secrets)
        if profile == "kpi":
            return validate_kpi(payload, secrets)
    except RefreshError as exc:
        exc.status = status
        exc.excerpt = excerpt
        exc.content_type = shown_type
        exc.retry = False
        raise
    raise RefreshError(f"unknown profile {profile}", retry=False)


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def urllib_transport(url: str, token: str, timeout: float) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirect,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            content_type = response.headers.get("Content-Type", "")
            body = response.read(MAX_BODY_BYTES + 1)
    except RefreshError:
        raise
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        raw = exc.read(MAX_BODY_BYTES + 1) if exc.fp is not None else b""
        body = raw or b""
    except (TimeoutError, socket.timeout) as exc:
        raise RefreshError(
            f"request timed out after {timeout:g} seconds",
            retry=True,
        ) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RefreshError(
                f"request timed out after {timeout:g} seconds",
                retry=True,
            ) from exc
        message = redact(str(reason), [token])
        raise RefreshError(f"network error: {message}", retry=True) from exc
    if len(body) > MAX_BODY_BYTES:
        raise RefreshError("response body exceeds 1 MB", retry=False, status=status)
    return status, content_type, body


def _attempt_log(attempt: int, max_attempts: int, err: RefreshError) -> str:
    lines = [f"Attempt {attempt}/{max_attempts}: {err.reason}"]
    if err.status is not None:
        lines.append(f"HTTP status: {err.status}")
    if err.content_type:
        lines.append(f"Content-Type: {err.content_type}")
    elif err.status == 200:
        lines.append("Content-Type: (missing)")
    if err.excerpt:
        lines.append(f"Body excerpt: {err.excerpt}")
    return "\n".join(lines)


def execute(config: Config, transport: Transport, sleeper: Sleeper) -> Outcome:
    label = "Sales" if config.profile == "sales" else "KPI"
    skip = schedule_skip_message(
        event=config.event,
        now=config.now,
        require_chicago_hour=config.require_chicago_hour,
        slots=config.require_chicago_slots,
        tolerance_minutes=config.chicago_slot_tolerance_minutes,
        schedule=config.schedule,
    )
    if skip is not None:
        return Outcome(kind="skipped", attempts=0, logs=[], message=skip)
    path = normalize_path(config.path, config.profile)
    if path == "":
        return Outcome(
            kind="skipped",
            attempts=0,
            logs=[],
            message=(
                "KPI refresh skipped: KPI_REFRESH_PATH is unset. Set the repository "
                f"variable KPI_REFRESH_PATH to {DEFAULT_KPI_PATH} after the app "
                f"implements POST {DEFAULT_KPI_PATH}. No request was sent."
            ),
        )
    if config.token == "":
        source = config.token_source or "the configured secret"
        raise RefreshError(
            f"secret {source} is unset. Add the bearer token in GitHub Actions secrets. "
            "The token value is not printed.",
            retry=False,
        )
    base_url = config.base_url
    url = f"{base_url}{path}"
    parts = urlsplit(base_url)
    target = f"{parts.scheme}://{parts.netloc}{path}"
    secrets = [config.token]
    logs: list[str] = []
    last_error: Optional[RefreshError] = None
    for attempt in range(1, config.max_attempts + 1):
        delay = backoff_before(attempt, config.backoff)
        if delay > 0:
            logs.append(f"Transient failure. Waiting {delay:g} seconds before retry.")
            sleeper(delay)
        try:
            status, content_type, body = transport(url, config.token, config.timeout)
            summary = evaluate_response(status, content_type, body, config.profile, secrets)
        except RefreshError as err:
            last_error = err
            logs.append(redact(_attempt_log(attempt, config.max_attempts, err), secrets))
            if err.retry and attempt < config.max_attempts:
                continue
            retry_note = f" after {attempt} attempts" if attempt > 1 else ""
            return Outcome(
                kind="failed",
                attempts=attempt,
                logs=logs,
                message=redact(
                    f"{label} refresh failed{retry_note}: {err.reason}. POST {target}",
                    secrets,
                ),
            )
        logs.append(
            f"Attempt {attempt}/{config.max_attempts}: HTTP 200 application/json ok=true"
        )
        return Outcome(
            kind="success",
            attempts=attempt,
            logs=[redact(line, secrets) for line in logs],
            message=redact(
                (
                    f"{label} refresh succeeded.\n"
                    f"Token source: {config.token_source}\n"
                    f"POST {target}\n"
                    f"{summary}"
                ),
                secrets,
            ),
        )
    reason = last_error.reason if last_error else "unknown failure"
    return Outcome(
        kind="failed",
        attempts=config.max_attempts,
        logs=[redact(line, secrets) for line in logs],
        message=redact(
            f"{label} refresh failed after {config.max_attempts} attempts: {reason}",
            secrets,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("sales", "kpi"))
    parser.add_argument("--path", default="")
    parser.add_argument(
        "--path-fallback",
        default="",
        help="Used when --path is empty. The KPI workflow passes KPI_REFRESH_PATH here.",
    )
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--token-env-fallback", default="")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--backoff-seconds", type=parse_backoff, default=(10, 30))
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "local"))
    parser.add_argument("--require-chicago-hour", type=int)
    parser.add_argument(
        "--require-chicago-slots",
        default="",
        help="Comma-separated America/Chicago slots such as Mon-00:15,Mon-04:15,Mon-05:00.",
    )
    parser.add_argument(
        "--chicago-slot-tolerance-minutes",
        type=int,
        default=DEFAULT_SLOT_TOLERANCE_MINUTES,
        help="Minutes a scheduled run may start before or after an allowed Chicago slot.",
    )
    parser.add_argument(
        "--schedule",
        default="",
        help="Cron that triggered a scheduled run, from github.event.schedule.",
    )
    parser.add_argument(
        "--test-hooks",
        action="store_true",
        help="Enable test-only clock control. Do not set this in GitHub Actions.",
    )
    parser.add_argument("--now", default="", help="Timezone-aware ISO time. Requires --test-hooks.")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    if args.now and not args.test_hooks:
        raise RefreshError("--now is available only with --test-hooks", retry=False)
    if args.timeout_seconds <= 0 or args.timeout_seconds > 180:
        raise RefreshError("timeout must be greater than 0 and at most 180 seconds", retry=False)
    if args.max_attempts < 1 or args.max_attempts > 5:
        raise RefreshError("max attempts must be from 1 to 5", retry=False)
    if args.require_chicago_hour is not None and not 0 <= args.require_chicago_hour <= 23:
        raise RefreshError("Chicago hour must be from 0 to 23", retry=False)
    slots = parse_chicago_slots(args.require_chicago_slots)
    if not 0 <= args.chicago_slot_tolerance_minutes <= 180:
        raise RefreshError(
            "Chicago slot tolerance must be from 0 to 180 minutes",
            retry=False,
        )
    schedule = " ".join(args.schedule.split())
    if schedule:
        parse_cron_schedule(schedule)
    now = None
    if args.now:
        text = args.now.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            now = datetime.fromisoformat(text)
        except ValueError as exc:
            raise RefreshError("--now must be an ISO-8601 timestamp", retry=False) from exc
        if now.tzinfo is None:
            raise RefreshError("--now must include a timezone offset", retry=False)
    # Schedule and empty-path skips happen before the token is required.
    # A blank manual input falls through to the repository variable.
    path = args.path.strip() or args.path_fallback.strip()
    base_url = validate_base_url(args.base_url, args.allow_remote)
    token = ""
    token_source = args.token_env
    needs_token = True
    if (
        schedule_skip_message(
            event=args.event,
            now=now,
            require_chicago_hour=args.require_chicago_hour,
            slots=slots,
            tolerance_minutes=args.chicago_slot_tolerance_minutes,
            schedule=schedule,
        )
        is not None
    ):
        needs_token = False
    if args.profile == "kpi" and path == "":
        needs_token = False
    if needs_token:
        token, token_source = resolve_token(args.token_env, args.token_env_fallback)
    return Config(
        profile=args.profile,
        path=path,
        token=token,
        token_source=token_source,
        base_url=base_url,
        timeout=args.timeout_seconds,
        max_attempts=args.max_attempts,
        backoff=tuple(args.backoff_seconds),
        event=args.event,
        require_chicago_hour=args.require_chicago_hour,
        require_chicago_slots=slots,
        chicago_slot_tolerance_minutes=args.chicago_slot_tolerance_minutes,
        schedule=schedule,
        now=now,
    )


def print_outcome(outcome: Outcome) -> int:
    for line in outcome.logs:
        print(line, flush=True)
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if outcome.kind == "failed":
        print(outcome.message, file=sys.stderr, flush=True)
        if actions:
            print(f"::error::{annotation_safe(outcome.message)}", file=sys.stderr, flush=True)
        return 1
    print(outcome.message, flush=True)
    if actions and outcome.kind == "skipped":
        print(f"::notice::{annotation_safe(outcome.message)}", flush=True)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        config = config_from_args(args)
        outcome = execute(config, urllib_transport, time.sleep)
    except RefreshError as err:
        outcome = Outcome(kind="failed", message=err.reason, logs=[], attempts=0)
    return print_outcome(outcome)


if __name__ == "__main__":
    sys.exit(main())
