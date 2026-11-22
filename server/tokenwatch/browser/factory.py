from .client import BrowserUpstreamClient
from .config import BrowserConfig


def browser_upstream(settings) -> BrowserUpstreamClient:
    config = BrowserConfig.from_toml(settings.browser_config, settings.signing_key.get_secret_value())
    return BrowserUpstreamClient(config)
