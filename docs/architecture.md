# Architecture

Python server on a Linux host; SwiftUI macOS client. Upstream data is documented in [arkham-upstream.md](arkham-upstream.md).

## Components

- **Browser pool**: one fingerprint-chromium instance per account (Windows persona with canvas spoofing disabled, own profile, own proxy), run under Xvfb. Requests are issued from inside the logged-in page. Why: the upstream requires a login session and blocks plain server requests.
- **Scheduler**: runs jobs per account with rate limits, jitter, and backoff. A paused account stops all its jobs.
- **Collectors**:
  - Daily balances: for every coin in the coin list, fetch entity balance changes once and store one row per entity per day.
  - Transfers: per subscribed coin, incremental fetch deduplicated by transfer id.
  - Entity Balance Changes (manual): triggered on demand, stored as a snapshot.
- **Rule engine**: evaluates rules from the database against new transfers and balance data, and emits events with a level.
- **Store**: SQLite in WAL mode.
- **API**: REST for queries and configuration, WebSocket for live events. `GET /coins/{coin_id}/entities?days=N` lists entities recorded in the inclusive UTC lookback window with their latest name, type, balance, USD value, and date; `GET /balances` returns selected entity curves and collection coverage.
- **macOS client**: menu bar app that views, filters, and configures data, and shows local notifications. It receives pushes only while running.

## Data flow

Scheduler → browser pool → collectors → store → rule engine → events → API/WebSocket → client.

## Coin list

Candidates are the union of trading Binance spot base assets and Binance Alpha symbols. Public feed requests have timeouts, bounded retries, and response validation. CoinGecko's public platform catalog is cached in SQLite, with refresh attempts at most once per week. Explicit chain/contract matches take priority over spot symbol matches; multiple matching IDs are ambiguous. Unsupported Alpha chains are not contract-matched. EVM contracts are case-insensitive; other contracts retain case.

Automatic mappings and ambiguity are persisted separately from manual overrides, which take priority. CoinGecko IDs are tentative Arkham pricing IDs, not proof of upstream support. New mappings remain unverified unless manually verified or an opt-in, rate-limited Arkham balance check (`limit=1`) returns data. The database retains ineligible candidates with their reasons. The client can edit mappings and exclusions.

Session status probes use signed `/user` requests through the account request gate. API reads share an in-memory TTL cache (`account_status_ttl`, default 600 seconds); successful scheduled requests and authentication failures also refresh it. A probe that fails for another reason returns `unknown`, is not cached, and does not count toward the account's failures or backoff; `Blocked`, `RateLimited` and `AuthRequired` during a probe pause the account like any other request.

## Login session

An authenticated client starts one interactive login session at a time with `POST /accounts/{id}/login`. The account is paused while its browser opens. `GET /accounts/{id}/login` reports `none`, `starting`, `open`, `closed`, or `failed`; launch failure restores its previous account status. The response contains a session-scoped secret ticket URL: HTTP assets at `/login-view/{ticket}/...` and the WebSocket at `/login-view/{ticket}/websockify` authenticate only with that ticket. Do not share or log it. `POST /accounts/{id}/login/finish` closes the browser, invalidates the ticket, activates the account, and performs one fresh rate-limited session status probe. After `login_timeout` seconds (default 900), the server closes the session and activates the account without a probe. See [deployment](deployment.md) for display and noVNC setup.

## Invariants

- Each account is bound to one proxy; there is no direct-connection fallback. Why: avoid exposing the server IP and mixing identities.
- Event rules, thresholds, and exclusions are data, not code. Why: they change without deployments.
- Transfer polling targets half the visible window per fetch, subject to configured polling and account rate limits. The first rate estimate is the returned count divided by its timestamp span; fewer than two rows or zero span uses the maximum interval. Once a nonempty baseline exists, a nonempty fetch that reaches no known transfer ID marks a gap and raises an alert. Why: the upstream exposes only the newest transfers.
- A balance fetch that returns the full page size is flagged as possibly truncated, never auto-paginated.
- Daily entity balances are kept indefinitely; snapshots and transfer history have configurable retention.
