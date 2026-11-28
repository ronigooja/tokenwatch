# Arkham upstream

Base URL: `https://api.arkm.com`. No official API key is used; requests run from a logged-in browser page.
Browser readiness and safe pre-fetch probing are described in [architecture.md](architecture.md#components).

## Access

- Requests need the headers `X-Timestamp` (unix seconds) and `X-Payload`.
- The client key is the literal assigned to `NEXT_PUBLIC_WEBAPP_CLIENT_KEY` in a Next.js static chunk of the arkm.com frontend, served as a shared Cloudflare-cacheable static file (`cache-control: public, max-age=31536000`); it is not in the page HTML or `__NEXT_DATA__`. The same asset bytes and key fingerprint were observed for two logged-in accounts in two browsers. Keep the key in private config, not in the repository.
- The request helper passes the key as the third argument to the signing function. `X-Payload` = `sha256(KEY + ":" + sha256(PATH + ":" + TIMESTAMP + ":" + KEY))`, where `PATH` is the URL pathname without query. Only the pathname, timestamp, and key are inputs; no cookie, token, user ID, or session is used, so the key does not depend on the account.
- Requests without a login session fail with `transfers.explorer denied at anonymous`.
- Requests from server IPs outside a page context are blocked by Cloudflare; the page must pass Cloudflare first.
- A human completes the interactive Cloudflare Turnstile on login.
- Browser Manager fetches are credentialed GETs, bound to the exact `https://arkm.com` page origin, with redirects disabled. Tokenwatch sends the same signed headers and does not retry a fetch on transport failure. See [startup behavior](architecture.md#components) for verification handling.
- With the fingerprint-chromium Windows persona and canvas spoofing enabled, the Turnstile checkbox on the login page ends in "Verification failed" after a real mouse click. With `--disable-spoofing=canvas`, or with no persona, the click ends in "Success!". Chrome 148 and 150 behave the same. Observed with one blank profile per configuration and one run each, through a residential proxy.

## Endpoints

All requests use GET and URL-encoded query strings. Numeric parameter types below describe their meaning; the client sends their decimal string representations. `pricing_id` is Arkham's `pricingID`. Responses are JSON.

| Path | Purpose and caller | Parameters sent | Response shape relied on |
|---|---|---|---|
| `/user` | Login detection: `RequestGate.account_status` and `BrowserUpstreamClient.account_status` | None | Nonempty object; specific keys and value types unobserved. Empty objects and other JSON values yield unknown status. |
| `/transfers` | Latest coin transfers: `Collectors.transfers` | `base`, `flow`: strings `all`; `sortKey`: string `time`; `sortDir`: string `desc`; `limit`: integer 100; `offset`: integer 0; `tokens`: pricing ID string | Object with `transfers`: array, at most 100 entries. |
| `/intelligence/entity_balance_changes` | Daily balances and [automatic snapshots](architecture.md#entity-balance-snapshots): `Collectors.daily_coin`, `Collectors.snapshots` | `interval`: string `7d`; `orderBy`: string `balance_usd_pct_change`; `orderDir`: string `desc`; `limit`: integer 50; `offset`: integer 0; `pricingIds`: one exact pricing ID; `balanceMin`: non-negative integer (daily only) | Array, at most 50 entries. Entries include balance and previous balance fields. |
| `/intelligence/search` | Contract matching and [catalog search](architecture.md#coin-list): `CoinCatalog.match_next`, `api.search_catalog` | `query`: string (contract address or free text, including symbol) | Object with optional `tokens`: array. Missing or null `tokens` means no address match; catalog search also treats a non-array as no candidates. |
| `/token/addresses/{pricing_id}` | Verify support: `CoinCatalog.match_next` | Path `pricing_id`: string; no query parameters | Object with nonempty chain-name string keys and opaque string values; `message` is not allowed in a successful response. An empty object is supported; its meaning as no token deployments is inferred. Empty objects are observed for native coins bitcoin, ethereum, solana, tron, avalanche-2, dogecoin, zcash, and hyperliquid. |

The upstream transfer window has `offset + limit` ≤ 100; upstream also supports an optional `usdGte` value filter, which the collector does not send. The upstream page sends a single balance request with `balanceMin=100000`; Tokenwatch does not page balance results. The upstream `limits` object's keys, types, and values are unobserved in code and tests.

### Search items

For address searches, a recognized item without `identifier.pricingID` has been observed with `identifier.chain` and `identifier.address`, without `price`; both matchers skip it. Items with a pricing ID have been observed with numeric `price`, `price24hAgo`, and `deployments`. Address matching associates a valid `identifier.pricingID` with matching `chain` and `address` in the identifier or deployments; it sends one request per usable deployment until resolved.

For symbol or other free-text searches, the parser uses `identifier.pricingID` as output `pricing_id`, optional string `name` and `symbol`, optional numeric `price`, `rank`, `marketCap`, and `market_cap`, and string deployment `chain` values as `chains`. It preserves symbol case, including lowercase; it does not require lowercase. It returns at most 20 candidates. Symbol queries have been observed returning items with a pricing ID, name, lowercase symbol, price, and deployment chains; other item fields are unobserved.

### Errors

HTTP 400 on `/token/addresses/{pricing_id}` with a JSON object whose only key is `message` means unsupported on that endpoint; it does not pause the account, count as a failure, or extend spacing. An invalid successful token-address response is `BadResponse`; its logged sanitized shape contains the top-level type, capped key and value-type counts, presence of `message`, capped empty-key count, and maximum string-length bucket, never body keys or values.

HTTP 401 or an anonymous-denial body maps to `AuthRequired`. HTTP 429 maps to `RateLimited`; a Cloudflare challenge maps to `Blocked`. Other HTTP 400/403 responses map to `RequestRejected`. These errors pause the account and apply backoff; `Retry-After` can extend rate-limit backoff. Pause events and warnings include sanitized `reject_detail` for `RequestRejected`: an object exposes up to eight cleaned key names capped at 24 characters and a cleaned string `message` capped at 120 characters; cleaning removes control characters and collapses whitespace. Non-object bodies expose only `json-nonobject` or `non-json` and a length bucket (`<=256`, `<=2048`, or `>2048` characters). Key names and messages remain upstream-controlled and can contain sensitive information. Other non-success statuses and invalid JSON map to `BadResponse`.

For account gate, status cache, retry ordering, and matching configuration, see [architecture.md](architecture.md).

## Transfer fields used

The collector reads `id`, `transactionHash`, `chain`, `fromAddress`, `toAddress` (each with `address`, `chain`, optional `arkhamEntity`), and `blockTimestamp`. Upstream transfer fields also include `arkhamLabel`, `tokenAddress`, `tokenSymbol`, `unitValue`, and `blockNumber`; the collector does not read them.

The server reads USD values from `historicalUSD` or `usdValue` if supplied; otherwise the USD value is unknown.

## Interactive authentication pages

Login: `https://arkm.com/login?redirectDomain=https://arkm.com`. Registration: `https://arkm.com/register?redirectDomain=https://arkm.com`. The registration page has `input#email[name=email][type=email]`, `input#password[name=password][type=password]`, and a “Sign Up” button. The login page DOM is not verified because a Cloudflare challenge can appear first. Local browser mode uses `input[type=email], input[name=email]` and `input[type=password]` to fill saved credentials when visible; it never submits the form. Browser Manager login autofill is described in [login session](architecture.md#login-session).
