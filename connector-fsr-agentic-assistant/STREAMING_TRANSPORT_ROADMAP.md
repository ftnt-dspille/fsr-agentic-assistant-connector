# Roadmap — real streaming transport via a custom nginx route

_Status: idea / not scheduled. Captured 2026-07 from a design discussion.
This is deliberately NOT a near-term task — it's the "right" long-term
transport, recorded so we don't rediscover it._

## ⚠️ Update — FortiSOAR 8.0 already ships the ASGI-service pattern

A live nginx audit of an 8.0 appliance (Rocky 9.8, system Python 3.9.25)
changes the framing below. **8.0 ships its own agentic-AI stack with dedicated
nginx routes** added directly into the vendor `cyops-api.conf` (`:443` server):

- **`location /mcp/` → `http://localhost:8010`** — the MCP route for agentic AI
  features (`mcp-server.service`, "MCP Service"). Auth is **Bearer header**, not
  the `pb-stats` `auth_request`/cookie gate — unauthenticated it returns
  `{"message":"Missing Authorization header"}`.
- **`location /ai/` → `https://localhost:8001`** — the "Agentic AI" service
  (`fsr-ai.service` + `fsr-ai-celery.service`, running
  `/opt/fsr-ai/.venv/bin/python3 /opt/fsr-ai/main.py`). This is exactly the
  `fsr-ai` FastAPI/uvicorn template named below, now a **first-class shipped
  component**. Its OpenAPI self-describes as _"Autonomous SOC Analyst
  (Definitive Version)" v5.0.0_ (paths: `/ai/chat/`, `/ai/triage/alert`,
  `/ai/agents/*`, `/ai/insight/plan/execute` + `/{task_id}/status|result`,
  `/ai/llm/provider|config`). **The nginx block is already WS/stream-upgrade
  wired** (`proxy_http_version 1.1` + `Upgrade`/`Connection` set), and the
  OpenAPI contains `stream` params.

Implications for this roadmap:

1. The premise still holds for **our** connector — `chat_turn` is still a
   connector-execute op under harakiri and the widget still polls `chat_poll`.
2. But **"stand up a new appliance-resident ASGI service" is no longer novel** —
   8.0 ships that exact pattern as a supported component.
3. The upgrade-safe deploy path assumed below (`include
   /etc/nginx/custom/*.conf`) **does not exist on 8.0** — `/etc/nginx/custom/`
   is absent; Fortinet added the routes directly into vendor `cyops-api.conf`.
4. **Decision to make before building a custom `/api/fsr-chat-stream/` route:**
   (a) ride the shipped `/mcp/` or `/ai/` surface instead of maintaining our own
   poll transport, vs. (b) if we still build our own SSE route, model its auth
   on `/mcp/`'s **Bearer** scheme (what 8.0 actually uses), not the older
   `pb-stats` cookie gate. Note `/etc/nginx/custom/` is gone, so an upgrade-safe
   drop-in include is no longer available on 8.0 either.

_The original design below predates this discovery and is kept for the
transport mechanics; treat the nginx-deploy specifics as 3.9-era._

## Problem this solves

Today "streaming" is **client polling of a server-side buffer**:

- `chat_turn` is a connector-execute op — synchronous request/response on
  uwsgi workers with a per-request `harakiri` ceiling (~300s). It cannot hold
  a connection open or push.
- So `chat_turn` runs **detached** (a daemon thread) writing one wire frame
  per event into the SQLite `turn_progress` feed, and the widget polls
  `chat_poll` every ~700ms, each poll a full authenticated connector-execute
  round-trip.

Consequences: ~1.4 authenticated round-trips/sec per active turn, up to ~700ms
added latency per frame, and a large amount of cursor / turn-fence /
stale-replay bookkeeping that exists **only because we poll a shared buffer**.

**Python version is NOT the blocker.** py3.12 gives nicer asyncio
(`TaskGroup`, `asyncio.timeout`) and ~10-15% CPU, but no new transport. The
blocker is the connector-execute model itself.

## The unlock: a custom nginx-fronted loopback service

A custom nginx `location` that `proxy_pass`es to our own long-running loopback
process escapes both constraints: no harakiri ceiling, and a real streaming
transport (SSE `text/event-stream`, or a websocket). This pattern is already
proven in-house — see the **`pb-stats`** example (playbook step-count API):

- `troubleshooting/pb-stats.nginx.snippet` — `location ^~ /api/pb-stats/`
  with an `auth_request` subrequest gate + `proxy_pass http://127.0.0.1:8088/`.
- `troubleshooting/pb_stats_service.py` — a **stdlib `http.server`** loopback
  service (zero pip deps — runs on a 3.9 appliance as-is).
- `troubleshooting/_setup_nginx_include.py` — upgrade-safe deploy: one
  `include /etc/nginx/custom/*.conf;` line in the `:443` server of
  `cyops-api.conf`; route files live in `/etc/nginx/custom/` so vendor
  upgrades don't clobber them.
- `fsr-ai` (`/ai/` -> uvicorn on :8001) is the FastAPI/ASGI variant, also
  blessed on-appliance.

Live-verified: the `pb-stats` route + its loopback service run in production on
a 3.9 FortiSOAR appliance (Rocky 9, system Python 3.9) — the exact class of box
we need this to work on.

## Design (reuses today's code — transport swap, not a rewrite)

Keep the connector owning the agent turn; move only the transport.

```
chat_turn (connector op, detached thread)      ← UNCHANGED: Anthropic/OpenAI turn,
   └─ writes per-event frames → SQLite turn_progress   tier gating, approval gateway
                                     │
   NEW loopback service (pb-stats pattern) tails that SAME feed:
   GET /api/fsr-chat-stream/?session_id=…&since_turn=…  →  text/event-stream
                                     │  (nginx proxy_pass, buffering OFF)
   Widget: fetch()+ReadableStream (or EventSource) instead of the 700ms poll loop
```

- The stream service reads the feed through the **same
  `read_turn_progress(session_id, cursor, since_turn)`** contract, so the
  cursor / turn-fence / `reserve_next_turn` machinery carries over verbatim.
- It internally polls SQLite at ~100-150ms (loopback — no HTTP/auth cost) and
  pushes each new frame to the held browser connection, closing on the
  terminal frame.
- One held connection replaces thousands of authenticated `chat_poll`
  round-trips per build; frame latency drops from ~700ms to near-real-time.
- Escapes harakiri: the held request is **nginx -> loopback service**, never a
  connector-execute op.

### nginx block = the pb-stats block + SSE-specific lines

```nginx
location ^~ /api/fsr-chat-stream/ {
    auth_request /_pbstats_auth;          # reuse the existing FSR auth gate
    proxy_pass http://127.0.0.1:8089/;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    proxy_buffering   off;                # critical: stream, don't buffer
    proxy_read_timeout 3600s;             # allow a long build to run
    add_header X-Accel-Buffering no;      # belt-and-suspenders vs buffering
}
```

## Real gotchas to resolve during a spike (none are the Python version)

1. **Browser auth on the stream request.** `EventSource` can't set an
   `Authorization` header, so either the `auth_request` gate must accept the
   browser's same-origin **FSR session cookie** (the widget runs same-origin,
   so the cookie rides along — the existing `pb-stats` gate hits
   `/api/auth/license`; confirm it passes cookie-only), or use
   `fetch()` + `ReadableStream` in the widget so headers can be set. Prefer
   fetch-stream — more control, and it's what decides the auth shape.
2. **Session ownership.** The service must confirm the caller owns
   `session_id` (not merely that the token/license is valid), or one analyst
   could tail another's feed.
3. **Progressive enhancement.** Widget tries the SSE route; on
   404 / connection-refused it falls back to today's `chat_poll` loop. Keeps
   older boxes (or a box without the route deployed) working unchanged, and
   makes this shippable incrementally.
4. **New appliance-resident component.** A loopback service = systemd unit
   (run as `nginx:nginx`), upgrade-survival via `/etc/nginx/custom/`, and its
   own deploy path. Real ops surface — but identical to `pb-stats` / `fsr-ai`.

## Service flavor by box

- stdlib `http.server` (the `pb-stats` template) — zero deps, runs on the 3.9
  appliance as-is; SSE is trivial (`write("data: …\n\n"); flush()`). **Safe
  default.**
- uvicorn/FastAPI (`fsr-ai` template) — nicer async, but needs the stack
  installed; fine on newer boxes.

## Cheaper interim wins (do these first, no new component)

If we want most of the streaming feel without standing up a service:

- **Long-poll `chat_poll`**: block server-side up to ~3-5s on the feed and
  return the instant a new frame lands (or on timeout). Cuts round-trips
  ~5-10x, stays under harakiri, contract-compatible behind an opt-in
  `long_poll_ms` param. Watch uwsgi worker occupancy under concurrency.
- **Adaptive poll backoff** in the widget: widen when idle, tighten during
  token flow.
- Perf items already logged in the widget PLAN: `_mergeUiState` rebuilds id
  maps every poll (O(n^2) over a long build); `_absorbUsage` rescans all
  events per append — both cache-able.

## Suggested path when picked up

1. Ship the interim long-poll + adaptive backoff (small, low-risk, big cost
   drop). Measure worker saturation.
2. Spike the SSE route on a dev appliance using the `pb-stats` template;
   resolve gotcha #1 (browser auth) first — it gates everything.
3. Widget fetch-stream client behind the 404 fallback.
4. Decide whether the SSE win justifies retiring the poll machinery, or
   whether long-poll is "good enough" and the route stays optional.
