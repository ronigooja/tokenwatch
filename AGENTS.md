# Repository rules

- When handling credentials (accounts, passwords, proxies, tokens, signing keys), keep them out of the repository because it is public; store them only in git-ignored config and provide `*.example` templates.
- When sending requests to Arkham, apply rate limiting and backoff, pause and alert on abnormal responses, and never send requests to probe limits or use production accounts to test request rates.
- When implementing event rules, thresholds, or excluded coins, store them in the database and put no default values in code.
- When changing Arkham requests or finding behavior that differs from [docs/arkham-upstream.md](docs/arkham-upstream.md), update that document in place in the same change.
- When writing documentation, use English and describe only current facts; omit history, progress, dates, and rejected options, define each fact once, and link elsewhere.
- When working on Arkham requests, read [docs/arkham-upstream.md](docs/arkham-upstream.md); when working on overall structure, read [docs/architecture.md](docs/architecture.md).
