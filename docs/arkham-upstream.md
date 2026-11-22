# Arkham upstream

Base URL: `https://api.arkm.com`. No official API key is used; requests run from a logged-in browser page.

## Access

- Requests need the headers `X-Timestamp` (unix seconds) and `X-Payload`.
- `X-Payload` = `sha256(KEY + ":" + sha256(PATH + ":" + TIMESTAMP + ":" + KEY))`, where `PATH` is the URL path without query and `KEY` is the client key embedded in Arkham's frontend. Keep `KEY` in config, not in code.
- Requests without a login session fail with `transfers.explorer denied at anonymous`.
- Requests from server IPs outside a page context are blocked by Cloudflare; the page must pass Cloudflare first.
- The login page shows an interactive Cloudflare Turnstile; scripted clicks did not pass it, so login is completed by a human.

## Endpoints

| Endpoint | Use | Limits |
|---|---|---|
| `GET /transfers?base=all&flow=all&sortKey=time&sortDir=desc&limit=N&offset=M&tokens=COIN` | Latest transfers of a coin; optional `usdGte` filters by value | `offset + limit` must be ≤ 100 |
| `GET /intelligence/entity_balance_changes?interval=7d&orderBy=balance_usd_pct_change&orderDir=desc&balanceMin=USD&pricingIds=COIN&limit=50&offset=0` | Entities holding a coin with balance and previous balance (`balanceUnit`, `balanceUsd`, `prevBalanceUnit`, `prevBalanceUsd`) | The page sends one request with `balanceMin=100000`, `limit=50`, no paging |

- `COIN` is Arkham's `pricingID` (for example `brevis`, `avalanche-2`).
- Omitting `pricingIds` returns entities with per-coin `tokenBalances`.
- `interval` values used by the page: `7d`, `14d`, `30d`.
- Responses are JSON; transfers are under `transfers`, balance changes are a top-level array.

## Transfer fields used

`id`, `transactionHash`, `fromAddress`, `toAddress` (each with `address`, `chain`, optional `arkhamEntity` and `arkhamLabel`), `tokenAddress`, `tokenSymbol`, `unitValue`, `blockTimestamp`, `blockNumber`.
