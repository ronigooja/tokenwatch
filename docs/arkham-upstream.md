# Arkham upstream

Base URL: `https://api.arkm.com`. No official API key is used; requests run from a logged-in browser page.

## Access

- Requests need the headers `X-Timestamp` (unix seconds) and `X-Payload`.
- `X-Payload` = `sha256(KEY + ":" + sha256(PATH + ":" + TIMESTAMP + ":" + KEY))`, where `PATH` is the URL path without query and `KEY` is the client key embedded in Arkham's frontend. Keep `KEY` in config, not in code.
- Requests without a login session fail with `transfers.explorer denied at anonymous`.
- Requests from server IPs outside a page context are blocked by Cloudflare; the page must pass Cloudflare first.
- A human completes the interactive Cloudflare Turnstile on login.
- With the fingerprint-chromium Windows persona and canvas spoofing enabled, the Turnstile checkbox on the login page ends in "Verification failed" after a real mouse click. With `--disable-spoofing=canvas`, or with no persona, the click ends in "Success!". Chrome 148 and 150 behave the same. Observed with one blank profile per configuration and one run each, through a residential proxy.

## Endpoints

| Endpoint | Use | Limits |
|---|---|---|
| `GET /user` | Login detection from the existing page context, with the same signing and account request gate | Logged-in sessions return HTTP 200 and user JSON; anonymous requests are denied. Login detection relies on HTTP status, not specific user fields; the user response schema is not verified. |
| `GET /transfers?base=all&flow=all&sortKey=time&sortDir=desc&limit=N&offset=M&tokens=COIN` | Latest transfers of a coin; optional `usdGte` filters by value | `offset + limit` must be ≤ 100 |
| `GET /intelligence/entity_balance_changes?interval=7d&orderBy=balance_usd_pct_change&orderDir=desc&balanceMin=USD&pricingIds=COIN&limit=50&offset=0` | Entities holding a coin with balance and previous balance (`balanceUnit`, `balanceUsd`, `prevBalanceUnit`, `prevBalanceUsd`) | The page sends one request with `balanceMin=100000`; daily jobs configure their own minimum, and catalog checks omit it. No paging. |

- `COIN` is Arkham's `pricingID` (for example `brevis`, `avalanche-2`).
- Omitting `pricingIds` returns entities with per-coin `tokenBalances`. Manual snapshots encode multiple pricing IDs as one comma-separated parameter.
- `interval` values used by the page: `7d`, `14d`, `30d`.
- Responses are JSON; transfers are under `transfers`, balance changes are a top-level array.
- Login probes require a nonempty JSON object after successful HTTP decoding; they do not assume an account identifier field name. Status cache behavior is defined in [architecture.md](architecture.md).

## Transfer fields used

`id`, `transactionHash`, `fromAddress`, `toAddress` (each with `address`, `chain`, optional `arkhamEntity` and `arkhamLabel`), `tokenAddress`, `tokenSymbol`, `unitValue`, `blockTimestamp`, `blockNumber`.

The server reads USD values from `historicalUSD` or `usdValue` if supplied; otherwise the USD value is unknown.
