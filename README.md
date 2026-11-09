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

KPIs are weekly. The workflow runs on Monday only, at three America/Chicago times, and it can also be started with `workflow_dispatch`.

| Chicago time | Why this time | Central Daylight Time (UTC-5) | Central Standard Time (UTC-6) |
| --- | --- | --- | --- |
| 12:15 a.m. Monday | La Viva can be final after midnight. | `15 5 * * 1` (05:15 UTC Monday) | `15 6 * * 1` (06:15 UTC Monday) |
| 4:15 a.m. Monday | Two Zalat stores close around 3 a.m., so Zalat sales and labor are final only after 4 a.m. | `15 9 * * 1` (09:15 UTC Monday) | `15 10 * * 1` (10:15 UTC Monday) |
| 5:00 a.m. Monday | Final refresh. Everything must be up by 5 a.m. | `0 10 * * 1` (10:00 UTC Monday) | `0 11 * * 1` (11:00 UTC Monday) |

12:15 a.m. Chicago is still Monday in UTC under both offsets. GitHub cron is UTC and does not follow America/Chicago daylight-saving time, so each local time is registered twice. The scheduled job passes `--require-chicago-slots Mon-00:15,Mon-04:15,Mon-05:00` and the cron that started the run. The run continues only when that cron's Chicago clock time is one of those Monday slots and the job starts within ±40 minutes of the cron. The other daylight-saving registration exits successfully without calling the app. A manual `workflow_dispatch` bypasses the guard. `--require-chicago-hour` remains available and still limits a scheduled run to one America/Chicago hour. This workflow passes the Monday slot list.

GitHub may start a scheduled run later than the cron time. The ±40 minute window covers that delay. A run that starts further away exits successfully without calling the app. 4:15 a.m. and 5:00 a.m. are 45 minutes apart, so the guard uses the triggering cron. A ±40 minute window around local time alone would treat the other offset's neighboring registration as a match.

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
