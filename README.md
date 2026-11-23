# 1836 scoreboard refresh

This public repository is the GitHub Actions scheduler for the 1836 Scoreboard dashboard at `https://lagoon-brook-orchid-pearl.grok.me`. The dashboard source lives in the Grok Build app. This repo only sends the refresh requests below.

Secret values stay in GitHub Actions secrets. Do not put a token in this repo, a workflow log, an issue, or a pull request.

## Live sales

Workflow: `.github/workflows/refresh-scoreboard.yml`

The workflow runs about every 5 minutes (`*/5 * * * *`) and from a manual `workflow_dispatch`. GitHub may start a scheduled run later than the cron time. The concurrency group `scoreboard-sales-refresh` keeps one run active and keeps only the newest waiting run, so refreshes do not overlap.

On Mondays between 10:20 and 12:30 UTC, a second job in this workflow checks whether `refresh-kpis.yml` already has a successful run for the current UTC day. If it does not, and no run is already in progress, it dispatches `refresh-kpis.yml` with `GITHUB_TOKEN`. That window is 4:20-6:30 a.m. Central Standard Time and 5:20-7:30 a.m. Central Daylight Time, so a dropped weekly cron can still be replaced before 7:00 a.m. America/Chicago in either season. The job needs `actions: write` for the dispatch. Duplicate KPI runs are harmless: the endpoint is idempotent, and the `scoreboard-kpi-refresh` concurrency group keeps an in-progress run and does not cancel it.

Each attempt is a `POST` to `/api/refresh-sales` with a 60-second timeout. The bearer token comes from the secret `SCOREBOARD_REFRESH_TOKEN`. A missing secret fails the run with that name in the log and does not print a value.

Success is all of the following:

- HTTP 200
- a content type of `application/json` (a charset parameter is fine)
- a JSON object whose `ok` field is the boolean `true`

An HTML page, including one returned with HTTP 200, is a failure and is not retried. A JSON body whose `ok` value is the string `"true"` is also a failure. Redirects are not followed.

These statuses are retried, up to 3 attempts, with a 10-second wait and then a 30-second wait: network errors, timeouts, and HTTP 408, 429, 500, 502, 503, and 504. HTTP 401, 403, 404, other 4xx responses, redirects, and a 200 that fails the JSON check stop on the first attempt.

The failure log shows the attempt number, HTTP status, content type, a short redacted body excerpt, and the reason the check failed.

### Sales response the app already returns

- Method: `POST`
- Path: `/api/refresh-sales`
- Header: `Authorization: Bearer <SCOREBOARD_REFRESH_TOKEN>`
- Header: `Accept: application/json`
- HTTP 503 when the app token is unset, HTTP 401 when the bearer token is invalid, HTTP 502 when the sales pull fails, HTTP 200 on success
- Success body fields: `ok`, `run`, `hungerIncluded`, `hungerRule`, `businessDate`, `updatedAt`, `toastCollectedAt`, `hungerCollectedAt`, `lastError`

The scheduler decides pass or fail from `ok: true` and the JSON content type. It prints the other fields when they are present.

## KPI snapshot

Workflow: `.github/workflows/refresh-kpis.yml`

KPIs are weekly. The snapshot must refresh on Monday and be finished before 7:00 a.m. America/Chicago. The workflow also starts from a manual `workflow_dispatch`.

GitHub cron is UTC and does not follow America/Chicago daylight-saving time. Scheduled runs are often delayed and are sometimes dropped, so the workflow registers four Monday UTC times plus a later backup. Each of the first four is Monday morning in both Central Standard Time (UTC-6) and Central Daylight Time (UTC-5). At least one falls between about 4:30 and 6:30 a.m. Chicago in either season. The minutes are 15 and 45 so the runs avoid the start of the hour, when GitHub delays scheduled workflows. The latest on-time start among those four is 6:45 a.m. Chicago, and the job timeout is 10 minutes, so an on-time run finishes before 7:00 a.m.

A fifth Monday cron at 12:15 UTC is a last-resort backup if those four scheduled starts are all dropped. That is 6:15 a.m. Central Standard Time and 7:15 a.m. Central Daylight Time. During daylight time it starts after the 7:00 a.m. deadline. The earlier backup is the live-sales workflow, which can dispatch this workflow on Mondays between 10:20 and 12:30 UTC when no successful run exists yet for that UTC day.

| UTC Monday | Central Standard Time (UTC-6) | Central Daylight Time (UTC-5) |
| --- | --- | --- |
| `15 10 * * 1` (10:15 UTC) | 4:15 a.m. | 5:15 a.m. |
| `45 10 * * 1` (10:45 UTC) | 4:45 a.m. | 5:45 a.m. |
| `15 11 * * 1` (11:15 UTC) | 5:15 a.m. | 6:15 a.m. |
| `45 11 * * 1` (11:45 UTC) | 5:45 a.m. | 6:45 a.m. |
| `15 12 * * 1` (12:15 UTC) | 6:15 a.m. (backup) | 7:15 a.m. (backup) |

The scheduled job passes `--require-chicago-monday`. It sends the request when the current America/Chicago time is a Monday, at any hour, so a late start still refreshes. A scheduled run that starts on another weekday does not call the app and exits non-zero, which makes the miss visible. The refresh endpoint is idempotent, so the extra Monday runs are safe. A manual `workflow_dispatch` bypasses the Monday guard and can run on any day. `--require-chicago-hour` remains available and still limits a scheduled run to one America/Chicago hour. This workflow does not pass that flag.

The concurrency group is `scoreboard-kpi-refresh`.

The request path is the `workflow_dispatch` input `kpi_path` when that input contains a path. A blank input uses the repository variable `KPI_REFRESH_PATH`. When the resulting path is empty, the job prints a notice and exits successfully. It does not call the app. Set `KPI_REFRESH_PATH` to `/api/refresh-kpis` after the app implements the contract below.

Each attempt uses a 120-second timeout, the same 3-attempt backoff as sales, and the same HTTP success rule. The KPI profile also requires the JSON fields in the contract below. The bearer token is the secret `SCOREBOARD_KPI_REFRESH_TOKEN` when that secret is set, and otherwise the secret `SCOREBOARD_REFRESH_TOKEN`.

## Names to configure

Secrets, under Settings, Secrets and variables, Actions, Secrets:

| Name | Use |
| --- | --- |
| `SCOREBOARD_REFRESH_TOKEN` | Bearer token for live sales. Fallback bearer token for the KPI snapshot. |
| `SCOREBOARD_KPI_REFRESH_TOKEN` | Optional bearer token for the KPI snapshot. Used instead of `SCOREBOARD_REFRESH_TOKEN` when set. |

Repository variable:

| Name | Use |
| --- | --- |
| `KPI_REFRESH_PATH` | Path for the KPI workflow. Use `/api/refresh-kpis` once the app exposes it. An empty or missing value skips the KPI job. |

The host is fixed to `lagoon-brook-orchid-pearl.grok.me`. The path has to be a single absolute path on that host.

## KPI endpoint the app needs to implement

- Method: `POST`
- Path: `/api/refresh-kpis`
- Header: `Authorization: Bearer <SCOREBOARD_KPI_REFRESH_TOKEN>` when that secret is configured, otherwise `Authorization: Bearer <SCOREBOARD_REFRESH_TOKEN>`
- Header: `Accept: application/json`
- Empty request body
- Success status: HTTP 200
- Success content type: `application/json`
- Success JSON:

```json
{
  "ok": true,
  "builtAt": "2026-11-09T12:05:00Z",
  "weeksUpdated": 1,
  "sources": {
    "toast": {
      "lastSuccess": "2026-11-09T12:00:00Z",
      "coverage": "2026-11-03/2026-11-09",
      "stale": false
    }
  },
  "lastError": null
}
```

Field rules the scheduler enforces:

- `ok`: boolean `true`. Any other JSON value fails the run.
- `builtAt`: ISO-8601 timestamp string. Include the time, not only a date.
- `weeksUpdated`: integer greater than or equal to 0.
- `sources`: object keyed by source name. Each value is an object with `lastSuccess` (ISO-8601 timestamp or `null`), `coverage` (string or `null`), and `stale` (boolean). An empty object is valid.
- `lastError`: string or `null`.

Use the same status codes as sales so the scheduler can classify them: HTTP 503 when the app token is unset, HTTP 401 when the bearer token is missing or invalid, and HTTP 502 when the snapshot build fails. A JSON error body with `ok: false` and `lastError` set gives the Actions log a useful excerpt. The scheduler treats HTTP 503 as transient and tries it up to 3 times.

The scheduler does not follow redirects. A login page or any other HTML document is a failed refresh, including when the status is 200.

## Prior-week re-pull (KPI date range)

By default the KPI request has no date range and the app refreshes its usual recent days. Once the app accepts a range on `POST /api/refresh-kpis`, set the repository variable `KPI_RANGE_MODE` to `prior-and-current`. Every KPI run, scheduled or dispatched, then sends:

```
POST /api/refresh-kpis?start=<Monday of the prior America/Chicago week>&end=<today in America/Chicago>
```

On Monday Nov 23, 2026 that is `?start=2026-11-16&end=2026-11-23`. The body stays empty and the response contract above is unchanged. Leave `KPI_RANGE_MODE` unset or `none` until the app change is live.

A manual run can re-pull any span of up to 62 days with the `range_start` and `range_end` inputs (`YYYY-MM-DD`; an empty end means today). Explicit dates win over `KPI_RANGE_MODE`. Invalid dates, an end before the start, an end after today, or a span longer than 62 days fail the run before any request is sent.

Purchases (Zalat Lisanti, La Viva nightly workbook) and guest scores (Ovation) are not pulled by this repo. Box-side pushers send them to the app's token-authenticated `POST /api/ingest-data`.

## Local validation

```bash
python3 scripts/test_refresh_call.py
```

The tests bind to `127.0.0.1` and check the response rules, retries, skip behavior, and workflow files. They do not call the dashboard.

Scheduled workflows start only after the workflow file is on the default branch. A green local test means the checks in this repo passed. It does not mean a live sales or KPI refresh has succeeded.
