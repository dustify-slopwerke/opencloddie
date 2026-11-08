# Backlog

## OpenCode usage indicator

**Goal:** stacked horizontal bars at the top of the web UI showing Go usage for
the rolling (5-hour), weekly, and monthly windows.

### What the docs say

- Go limits are monthly dollar amounts per model.
- The windows are percentages of a model's monthly limit: **5-hour = 20%**,
  **weekly = 50%**, **monthly = 100%**.
- Every inference response includes `usage.opencode.allowance` — how much the
  request counted toward the plan, in dollars — plus `billing_source`.
- Console API keys can have a **Usage: Read** permission (Restricted keys:
  `Models: Request` + `Usage: Read`).

### Endpoint check (2026-11-08)

```
GET https://opencode.ai/console/api/activity/usage
  Authorization: Bearer <oc_sk_...>
```

| Key | Result |
| --- | --- |
| current inference key | `403 {"_tag":"Forbidden"}` |
| invalid key | `401 {"_tag":"Unauthorized"}` |
| no key | `401 {"_tag":"Unauthorized"}` |

So the endpoint is real, accepts bearer keys, and the current key lacks
**Usage: Read**. A few guessed paths (`/console/api/go/limits`,
`/console/api/usage`, …) are 404; the activity endpoint is the one.

### Next steps

1. In the OpenCode Console, edit the key (owner/admin) or create a new
   personal/service-account key with:
   - `Models: Request` (so inference still works), and
   - `Usage: Read`.
2. Paste it in opencloddie **Settings → OpenCode key** (never in chat). The
   same key can keep serving inference.
3. Capture one successful response from `/console/api/activity/usage` to learn
   its query parameters and shape (range, per-window fields, plan limits).
4. Implement:
   - `GET /api/usage` on the **web** listener (cookie/Remote-User auth), which
     decrypts the user's stored OpenCode key, calls the Console endpoint
     server-side, caches for ~60s, and returns a normalized payload.
   - On 403, return `usage_permission_required` so the UI can show a short
     "grant Usage: Read" state instead of bars.
   - Top-bar component: three stacked bars (rolling / weekly / monthly),
     with tooltips showing used/limit and reset time.
   - Do not expose the OpenCode key or raw console response to the browser
     beyond the normalized numbers.
5. Add tests with a fake usage upstream: permission-required, success, cache,
   malformed payload, and user scoping.

### Fallback if no Usage: Read is possible

Accumulate `usage.opencode.allowance` from gateway responses per user/model and
compare against the configured monthly limit. This only reflects traffic
through opencloddie, not other clients or the console, so label it "observed"
rather than account usage.

## Terminal tab order

Fixed: `Database.list_sessions` now orders by `created_at, id` instead of
`online, last_seen_at`, so heartbeat updates no longer reshuffle tabs. A
regression test covers it.

## Possible follow-ups

- Per-viewer terminal sizing policy (`window-size largest` vs `latest`) exposed
  as a session option.
- Debounce/`visualViewport` handling for mobile resize.
- Optional user-defined tab ordering / pinning.
- Usage alerts when a window crosses a configurable threshold.
