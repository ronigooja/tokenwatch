# Server

From `server/`, install with `python -m pip install -e '.[test]'`. Copy `config.example.toml` to `config.local.toml` and set the secrets there or through `TOKENWATCH_*` environment overrides.

Run `uvicorn tokenwatch.api:create_app --factory` with one worker. Set `upstream_factory` to a `module:factory` callable accepting `Settings` and returning an `UpstreamClient` to enable collection; without it only the local API runs.

Run `python -m pytest` for offline tests or `python -m tokenwatch.export_openapi` to regenerate `openapi.json`.
