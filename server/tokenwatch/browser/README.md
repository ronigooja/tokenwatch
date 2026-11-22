# Browser pool

See [architecture.md](../../../docs/architecture.md) and [arkham-upstream.md](../../../docs/arkham-upstream.md) for pool invariants and the upstream contract.

- Install the server package (`server/pyproject.toml`) in a Python 3.11 environment. Provide the fingerprint-chromium 150 binary and an X display (`DISPLAY`).
- Copy `config.example.toml` to a private, git-ignored `browser.local.toml`; the server reads it from `browser_config` and sets `upstream_factory = "tokenwatch.browser.factory:browser_upstream"`. Percent-encode special characters in proxy credentials. Keep profiles private; they contain session cookies.
- With `tokenwatch` on the Python import path, run `python -m tokenwatch.browser.login --account ID` from the directory containing `browser.local.toml`, or pass `--config PATH`. Stop the service or otherwise release that account's profile before opening the helper. Complete the login in X/VNC, then terminate the helper before the service uses the profile again.
- Use `BrowserUpstreamClient` as an async context manager, or call `aclose()` during service shutdown. The scheduler owns pacing and backoff. A confirmed forwarder exit blocks that account for the lifetime of the pool; recreating the pool is required to retry.
- `account_status` only inspects visible login/logout buttons or links in the page. It does not open menus or probe the API; without a matching control it returns `unknown`.
