from pathlib import Path

from tokenwatch.browser.factory import browser_upstream
from tokenwatch.config import Settings


def test_factory_uses_server_signing_key(tmp_path):
    (tmp_path / "browser.local.toml").write_text(
        'binary_path = "chrome"\nprofiles_root = "profiles"\nrequest_timeout = 60\nclose_timeout = 1\n'
        '[[accounts]]\nid = "a1"\nproxy_url = "socks5h://u:p@proxy.example:1080"\nfingerprint_seed = 1\n'
    )
    settings = Settings(bearer_token="t", signing_key="server-key", browser_config=tmp_path / "browser.local.toml")
    client = browser_upstream(settings)
    assert client._config.signing_key == "server-key"
    assert [a.id for a in client._config.accounts] == ["a1"]
    assert isinstance(client._config.profiles_root, Path)
