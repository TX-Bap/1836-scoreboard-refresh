# 1836 scoreboard refresh

This public repository is the GitHub Actions scheduler for the 1836 Scoreboard dashboard at `https://lagoon-brook-orchid-pearl.grok.me`. The dashboard source lives in the Grok Build app. This repo only sends the refresh requests below.

Secret values stay in GitHub Actions secrets. Do not put a token in this repo, a workflow log, an issue, or a pull request.

## Live sales

Workflow: `.github/workflows/refresh-scoreboard.yml`

The workflow runs about every 5 minutes (`*/5 * * * *`) and from a manual `workflow_dispatch`. GitHub may start a scheduled run later than the cron time. The concurrency group `scoreboard-sales-refresh` keeps one run active and keeps only the newest waiting run, so refreshes do not overlap.

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

The workflow is scheduled for 6:05 a.m. America/Chicago and can also be started with `workflow_dispatch`. GitHub cron accepts UTC only. The file registers `5 11 * * *` (6:05 a.m. Central Daylight Time) and `5 12 * * *` (6:05 a.m. Central Standard Time). Each scheduled run continues only while the America/Chicago hour is 06, so one of those two times performs the refresh and the other exits without calling the app. A manual run ignores that hour gate. A scheduled run that starts after 7:00 a.m. Chicago exits without calling the app.

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

## Local validation

```bash
python3 scripts/test_refresh_call.py
```

The tests bind to `127.0.0.1` and check the response rules, retries, skip behavior, and workflow files. They do not call the dashboard.

Scheduled workflows start only after the workflow file is on the default branch. A green local test means the checks in this repo passed. It does not mean a live sales or KPI refresh has succeeded.
