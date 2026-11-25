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
- The effect of automatic mouse clicks on the full-page Cloudflare interstitial has not been verified on the real site; see [startup behavior](architecture.md#components).
- With the fingerprint-chromium Windows persona and canvas spoofing enabled, the Turnstile checkbox on the login page ends in "Verification failed" after a real mouse click. With `--disable-spoofing=canvas`, or with no persona, the click ends in "Success!". Chrome 148 and 150 behave the same. Observed with one blank profile per configuration and one run each, through a residential proxy.

## Endpoints

| Endpoint | Use | Limits |
|---|---|---|
| `GET /user` | Login detection from the existing page context, with the same signing and account request gate | Logged-in sessions return HTTP 200 and user JSON; anonymous requests are denied. Login detection relies on HTTP status, not specific user fields; the user response schema is not verified. |
| `GET /transfers?base=all&flow=all&sortKey=time&sortDir=desc&limit=N&offset=M&tokens=COIN` | Latest transfers of a coin; optional `usdGte` filters by value | `offset + limit` must be ≤ 100 |
| `GET /intelligence/entity_balance_changes?interval=7d&orderBy=balance_usd_pct_change&orderDir=desc&balanceMin=USD&pricingIds=COIN&limit=50&offset=0` | Entities holding a coin with balance and previous balance (`balanceUnit`, `balanceUsd`, `prevBalanceUnit`, `prevBalanceUsd`) | The page sends one request with `balanceMin=100000`; daily jobs configure their own minimum. No paging. |
| `GET /intelligence/search?query=...` | Find the Arkham coin number for a contract address or free text via [catalog search](architecture.md#coin-list). HTTP 200 without `tokens` means no address match. A recognized address without a coin number has only `identifier.chain` and `identifier.address`, without `pricingID` or `price`, and is skipped. Items with a coin number carry `identifier.pricingID`, numeric `price` and `price24hAgo`, and `deployments`. Match usable identifier or deployment addresses and chains to the pricing ID. The symbol-query item shape is not yet observed. | Address matching makes one request per usable deployment until resolved; free-text search makes one request per call. |
| `GET /token/addresses/{pricing_id}` | Verify support for the Arkham coin number. HTTP 200 returns a nonempty object keyed by Arkham chain names with opaque string values. | HTTP 400 with a JSON object containing only `message` means unsupported on this endpoint alone. It does not pause the account, count as a failure, or extend spacing. Other HTTP 400/403 responses pause under the normal gate; auth denials, challenges, and HTTP 429 retain their usual handling. |

- `COIN` is Arkham's `pricingID` (for example `brevis`, `avalanche-2`).
- Omitting `pricingIds` returns entities with per-coin `tokenBalances`. Manual snapshots encode multiple pricing IDs as one comma-separated parameter.
- `interval` values used by the page: `7d`, `14d`, `30d`.
- Responses are JSON; transfers are under `transfers`, balance changes are a top-level array.
- Login probes require a nonempty JSON object after successful HTTP decoding; they do not assume an account identifier field name. Status cache behavior is defined in [architecture.md](architecture.md).
- Matching failure tracking, retry ordering, and configuration are defined in [architecture.md](architecture.md#coin-list); account gate and backoff behavior remains in effect for each request.

## Transfer fields used

`id`, `transactionHash`, `fromAddress`, `toAddress` (each with `address`, `chain`, optional `arkhamEntity` and `arkhamLabel`), `tokenAddress`, `tokenSymbol`, `unitValue`, `blockTimestamp`, `blockNumber`.

The server reads USD values from `historicalUSD` or `usdValue` if supplied; otherwise the USD value is unknown.

## Interactive authentication pages

Login: `https://arkm.com/login?redirectDomain=https://arkm.com`. Registration: `https://arkm.com/register?redirectDomain=https://arkm.com`. The registration page has `input#email[name=email][type=email]`, `input#password[name=password][type=password]`, and a “Sign Up” button. The login page DOM is not verified because a Cloudflare challenge can appear first. The browser uses `input[type=email], input[name=email]` and `input[type=password]` to fill saved credentials when visible; it never submits the form.
