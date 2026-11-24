# Architecture

Python server on a Linux host; SwiftUI macOS client. Upstream data is documented in [arkham-upstream.md](arkham-upstream.md).

## Components

- **Browser pool**: one fingerprint-chromium instance per account (Windows persona with canvas spoofing disabled, own profile, own proxy), run under Xvfb. Requests are issued from inside the logged-in page. Why: the upstream requires a login session and blocks plain server requests.
- Before a non-interactive instance becomes ready, it waits for the Cloudflare challenge to clear, then for page load and network idle; a network-idle timeout is tolerated, and a nonempty title or the site origin is required. `page_settle_seconds` bounds each load-state wait (default 15 seconds; 0 disables settling and the recovery wait). Before each upstream fetch, a separate origin probe is evaluated; if its execution context is destroyed, the browser waits for load and repeats that probe once. The fetch evaluation itself is never retried, avoiding duplicate upstream requests.
- **Scheduler**: applies per-account rate limits, jitter, and backoff. A job pinned to an account stops while that account is paused. For each request, a job with `account_id: null` selects an active account unless its cached session status is `logged_out`, ordered by earliest next allowed request time, then least recently used, then account ID. Unknown or absent cached status is eligible; distribution does not probe to discover accounts. A pause causes retry on another account; if no account can proceed, `wait_reason` and `next_run` describe the wait.
- Jobs expose `last_error`, a sanitized handler error category and endpoint, cleared on successful execution. Browser failures use `_BrowserFailure[<detail>] on <endpoint>`; detail includes the exception type, a sanitized first message line, and, when available, safe page location and challenge title. `last_error` is capped at 200 characters. Handler failures are logged at warning level without request parameters or response bodies.
- **Collectors**:
  - Daily balances: for every supported coin, fetch entity balance changes once with the job’s `balance_min` and store one row per entity per UTC day.
  - Transfers: per subscribed coin, incremental fetch deduplicated by transfer id.
  - Entity Balance Changes (manual): triggered on demand, stored as a snapshot.
- **Rule engine**: evaluates rules from the database against new transfers and balance data, and emits events with a level.
- **Store**: SQLite in WAL mode.
- **API**: REST for queries and configuration, WebSocket for live events. `GET /coins/{coin_id}/entities?days=N` lists entities recorded in the inclusive UTC lookback window with their latest name, type, balance, USD value, and date; `GET /balances` returns selected entity curves and collection coverage.
- **macOS client**: menu bar app that views, filters, and configures data, and shows local notifications. It receives pushes only while running.

## Data flow

Scheduler → browser pool → collectors → store → rule engine → events → API/WebSocket → client.

## Coin list

The `coin_catalog` job refreshes five public Binance feeds: spot `exchangeInfo` (trading pairs with spot trading allowed), Alpha token list (all entries), `getNetworkCoinAll` (networks and legal-money flag), `get-all-asset` (asset tags and legal-money flag), and USD-M `exchangeInfo` (futures). Each feed has a timeout and bounded retries. A failed request or invalid shape leaves the last validated snapshot intact; `GET /coin-catalog` reports `unknown` and the last successful refresh. An individual spot asset without usable network or asset metadata is `unknown_onchain`. An address is usable even when deposit and withdrawal are disabled; an addressless network is usable only when named for the asset and at least one flag is enabled.

Coin IDs use Binance chain namespaces. Alpha IDs are `alpha:<chainId>:<contract>`; the same display symbol on several chains remains several coins. Spot networks belong to one coin: select an enabled, addressless network named for the base asset if present; otherwise prefer a contract and select by chain and normalized address. The resulting key is `native:<chain>` or `spot:<chain>:<contract>`; assets without usable networks have a diagnostic `unknown:spot:<symbol>` key. Key segments containing characters outside ASCII letters, digits, `_`, `.`, and `-` are encoded as `~` followed by UTF-8 hex. EVM addresses are case-insensitive; other addresses retain case. Spot and Alpha identities stay separate because their network namespaces are not assumed equivalent. Spot labels apply to Alpha only when its symbol and contract are also present in the asset’s network data.

Only trading USDT-quoted and USDT-margined USD-M `PERPETUAL` contracts with `underlyingType=COIN` qualify. Futures base symbols match exactly before consulting `PUT /coin-catalog/futures-mappings/{base}` with `{"symbol":"…"}`. Configure `1000SHIB→SHIB`, `1000XEC→XEC`, `1000LUNC→LUNC`, `1000PEPE→PEPE`, `1000FLOKI→FLOKI`, `1000BONK→BONK`, `1000CHEEMS→CHEEMS`, and `1000000MOG→MOG` through that endpoint. Before enabling jobs, configure exclusions with `PUT /coin-catalog/exclusion-rules/{kind}/{value}` and `{"reason":"…"}`: use `flag/isLegalMoney` for fiat and `tag/bStocks`, `tag/tCommodities`, and `tag/stablecoin` for exchange labels. Do not exclude `RWA`. There are no seeded rules or mappings. `PUT /exclusions/{coin_id}` adds an individual manual exclusion; the `/exclusions` list and item endpoints return only manual exclusions and their stored reasons. `GET /coins` reports both manual and catalog-rule exclusions as `excluded`, with the manual reason taking precedence when both apply. Deleting a manual exclusion restores the computed catalog or matching status.

The catalog and daily jobs use the same lazy matching step. An eligible token starts `pending_match`: search its canonical contract once and store the result (`no_match`, `ambiguous`, or its Arkham coin number and matching chain names). `GET /coin-catalog/chains` exposes observations and conflicts for Alpha chain IDs. A matched coin enters `pending_support` until the support check produces `supported` or `unsupported`. An eligible native coin is `needs_manual_mapping`: `PUT /coins/{coin_id}/mapping` with `{"pricing_id":"…"}` supplies a manual mapping for the next batch to verify. Manual mappings do not bypass catalog exclusions, unknown network state, or `no_futures`. Only `supported` coins are collected. `GET /coins?status=…` filters by state.

Configure the required matching failure limit using `PUT /coin-catalog/settings` with `{"failure_limit":3}`; `GET` reads it. No value is seeded, and matching refuses to run without it. Pending coins are selected by least recent attempt (never attempted first), then ID. Search or support upstream failures increment `match_failures` and set `last_match_attempt`; success resets the count. At the limit the coin becomes `match_failed`, with the failure class and count as its reason. `POST /coins/{coin_id}/retry` resets its count and returns it to its pending step. These fields are returned by the coin API. For an any-account job, an upstream failure schedules the next run at the request gate's earliest eligible availability; if none can proceed, `wait_reason` describes the gate wait.

Daily balances and subscriptions use the current coin ID. Transfer history and events can also contain historical selectors; after a catalog schema reset, review coin-specific rules and assign current IDs to disabled transfer jobs and subscriptions before enabling them.

## Session status

Session status probes use signed `/user` requests through the account request gate. `GET /accounts/{id}/session` uses an in-memory TTL cache (`account_status_ttl`, default 600 seconds); successful scheduled requests and authentication failures also refresh it. The cache is lost on service restart. A probe that fails for another reason returns `unknown`, is not cached, and does not count toward the account's failures or backoff; `Blocked`, `RateLimited` and `AuthRequired` during a probe pause the account like any other request.

## Login session

Before each interactive login, the server best-effort clears Caps Lock on its display. An authenticated client starts one interactive login session at a time with `POST /accounts/{id}/login`. The account is paused while its browser opens. `GET /accounts/{id}/login` reports `none`, `starting`, `open`, `closed`, or `failed`; launch failure restores its previous account status. The response contains a session-scoped secret ticket URL: HTTP assets at `/login-view/{ticket}/...` and the WebSocket at `/login-view/{ticket}/websockify` authenticate only with that ticket. Do not share or log it. `POST /accounts/{id}/login/finish` closes the browser, invalidates the ticket, activates the account, and performs one fresh rate-limited session status probe. After `login_timeout` seconds (default 900), the server closes the session and activates the account without a probe. See [deployment](deployment.md) for display and noVNC setup.

## Invariants

### Shutdown

Shutdown closes the request gate and scheduler to new work, stops the scheduler loop, and drains running jobs and gated requests for up to `shutdown_grace_seconds` (default 30). Remaining tasks are cancelled and awaited; login sessions then close, followed by the browser pool and its local proxy forwarders, and finally the database. Failures observed while the gate or browser pool is closing do not penalize accounts or become job errors; reserved request spacing is retained. The systemd unit uses `KillMode=mixed` to keep proxy subprocesses alive during draining and `TimeoutStopSec=90`; configure the timeout above the grace period with room for cleanup.

### Accounts

Authenticated REST endpoints list, inspect, create, edit, pause/resume, and delete accounts. New managed accounts use the normalized login email as ID and the Windows persona; existing IDs remain valid. The private `accounts_file` (default `accounts.local.toml` beside the browser configuration) stores email, password, and proxy URL in plaintext, and authenticated responses return them for managed accounts. Static `[[accounts]]` cannot be edited except for status or deleted through the API. On creation and proxy changes, the server detects the timezone through that account’s proxy using `https://ipwho.is/`; detection failure leaves the account unchanged. Editing preserves the fingerprint seed and persona; `replace_account` atomically writes the managed file, drops the running instance under its account lock, and updates the pool. Account deletion requires that no job references it and preserves its profile directory. Interactive login leases do not consume the non-interactive instance limit and are not idle-evicted while active.

Account responses expose the last cached [session status](#session-status) and its Unix timestamp, regardless of age, or null for both when absent; reading them makes no upstream request.

- Each account is bound to one proxy; there is no direct-connection fallback. Why: avoid exposing the server IP and mixing identities.
- Event rules, thresholds, and exclusions are data, not code. Why: they change without deployments.
- Transfer polling targets half the visible window per fetch, subject to configured polling and account rate limits. The first rate estimate is the returned count divided by its timestamp span; fewer than two rows or zero span uses the maximum interval. Once a nonempty baseline exists, a nonempty fetch that reaches no known transfer ID marks a gap and raises an alert. Why: the upstream exposes only the newest transfers.
- A balance fetch that returns the full page size is flagged as possibly truncated, never auto-paginated.
- Daily entity balances are kept indefinitely; snapshots and transfer history have configurable retention.
