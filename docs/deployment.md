# Deployment

- Host: Linux with Python 3.11, `xvfb-run`, and the fingerprint-chromium 150 Linux build.
- Layout: code in `/opt/tokenwatch`, virtualenv in `/opt/tokenwatch/venv`, private config in `/etc/tokenwatch` (mode 600, owned by the `tokenwatch` user).
- Config files: `config.local.toml` from [server/config.example.toml](../server/config.example.toml) and `browser.local.toml` from [the browser example](../server/tokenwatch/browser/config.example.toml). Both stay out of the repository.
- Service: [deploy/tokenwatch.service](../deploy/tokenwatch.service) runs one uvicorn worker on `127.0.0.1:8000` under `xvfb-run`. Expose it to the client only through an HTTPS reverse proxy or an SSH tunnel.
- First login of an account: stop the service, run the login helper described in [the browser README](../server/tokenwatch/browser/README.md) under X/VNC, complete login, then start the service.
