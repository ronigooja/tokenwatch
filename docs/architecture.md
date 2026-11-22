# Architecture

Python server on a Linux host; SwiftUI macOS client. Upstream data is documented in [arkham-upstream.md](arkham-upstream.md).

## Components

- **Browser pool**: one fingerprint-chromium instance per account (Windows persona, own profile, own proxy), run under Xvfb. Requests are issued from inside the logged-in page. Why: the upstream requires a login session and blocks plain server requests.
- **Scheduler**: runs jobs per account with rate limits, jitter, and backoff. A paused account stops all its jobs.
- **Collectors**:
  - Daily balances: for every coin in the coin list, fetch entity balance changes once and store one row per entity per day.
  - Transfers: per subscribed coin, incremental fetch deduplicated by transfer id.
  - Entity Balance Changes (manual): triggered on demand, stored as a snapshot.
- **Rule engine**: evaluates rules from the database against new transfers and balance data, and emits events with a level.
- **Store**: SQLite in WAL mode.
- **API**: REST for queries and configuration, WebSocket for live events.
- **macOS client**: menu bar app that views, filters, and configures data, and shows local notifications. It receives pushes only while running.

## Data flow

Scheduler → browser pool → collectors → store → rule engine → events → API/WebSocket → client.

## Coin list

Candidates are the union of Binance spot and Binance Alpha symbols. Automatic eligibility requires a mapped pricing ID and a nonempty Arkham balance response; verification requests are opt-in. The database retains ineligible candidates with their reasons. The client can edit mappings and exclusions and explicitly mark a mapping verified.

## Invariants

- Each account is bound to one proxy; there is no direct-connection fallback. Why: avoid exposing the server IP and mixing identities.
- Event rules, thresholds, and exclusions are data, not code. Why: they change without deployments.
- Transfer polling targets half the visible window per fetch, subject to configured polling and account rate limits. Once a nonempty baseline exists, a nonempty fetch that reaches no known transfer ID marks a gap and raises an alert. Why: the upstream exposes only the newest transfers.
- A balance fetch that returns the full page size is flagged as possibly truncated, never auto-paginated.
- Daily entity balances are kept indefinitely; snapshots and transfer history have configurable retention.
