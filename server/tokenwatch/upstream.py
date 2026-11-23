from typing import Any, Protocol


TOKEN_ADDRESSES_PREFIX = "/token/addresses/"
UNSUPPORTED_TOKEN = object()


class UpstreamError(Exception):
    pass


class AuthRequired(UpstreamError):
    """The account session is logged out or denied as anonymous."""


class Blocked(UpstreamError):
    """Cloudflare challenge or HTTP 403 that is not an auth denial."""


class RateLimited(UpstreamError):
    """HTTP 429 or an equivalent throttling response."""

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BadResponse(UpstreamError):
    """Non-JSON body or an unexpected status."""

    def __init__(self, message: str = "", *, status: int | None = None):
        super().__init__(message)
        self.status = status


class RequestRejected(BadResponse):
    """Unexpected HTTP 400 or non-challenge HTTP 403."""


class UpstreamClient(Protocol):
    def account_config(self, account_id: str) -> Any: ...

    async def add_account(self, account: Any) -> None: ...

    async def remove_account(self, account_id: str) -> None: ...

    async def get_json(
        self, account_id: str, path: str, params: dict[str, str] | None = None
    ) -> Any:
        """GET `path` on the upstream as `account_id` and return the decoded JSON.

        Raises UpstreamError subclasses; never falls back to a direct connection.
        """
        ...

    async def account_status(self, account_id: str) -> str:
        """Return `logged_in`, `logged_out`, or `unknown` without side effects."""
        ...
