import math
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit


SPOOFING_FEATURES = frozenset({"font", "audio", "canvas", "clientrects", "gpu"})


def proxy_parts(url: str) -> tuple[str, int, str, str]:
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        user = unquote(parts.username or "", errors="strict")
        password = unquote(parts.password or "", errors="strict")
        if (
            parts.scheme == "socks5h" and host and port
            and re.fullmatch(r"[a-zA-Z0-9._:-]+", host)
            and parts.path in ("", "/") and not parts.query and not parts.fragment
            and 0 < len(user.encode()) <= 255 and 0 < len(password.encode()) <= 255
            and ":" not in user
        ):
            return host, port, user, password
    except (AttributeError, ValueError):
        pass
    raise ValueError("Invalid authenticated SOCKS5 proxy URL")


@dataclass(frozen=True, slots=True)
class AccountConfig:
    id: str
    proxy_url: str = field(repr=False)
    fingerprint_seed: int
    timezone: str = "America/New_York"
    persona: str = "none"
    disable_spoofing: tuple[str, ...] = ("canvas",)
    email: str | None = None
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.persona not in ("none", "windows"):
            raise ValueError("Invalid browser persona")
        object.__setattr__(self, "disable_spoofing", tuple(self.disable_spoofing))
        if not set(self.disable_spoofing) <= SPOOFING_FEATURES or len(set(self.disable_spoofing)) != len(
            self.disable_spoofing
        ):
            raise ValueError("Invalid browser disable_spoofing")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_@+.-]{0,127}", self.id) or ".." in self.id:
            raise ValueError("Invalid browser account ID")
        if type(self.fingerprint_seed) is not int or self.fingerprint_seed < 0:
            raise ValueError("Invalid browser fingerprint seed")
        if not re.fullmatch(r"[a-zA-Z0-9_+/-]+", self.timezone):
            raise ValueError("Invalid browser timezone")
        proxy_parts(self.proxy_url)


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    binary_path: Path
    profiles_root: Path
    signing_key: str = field(repr=False)
    accounts: tuple[AccountConfig, ...]
    max_instances: int = 1
    idle_timeout: float = 60
    cf_wait_seconds: float = 30
    page_settle_seconds: float = 15
    accounts_file: Path | None = None
    managed_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.signing_key, str) or not self.signing_key:
            raise ValueError("Missing signing key")
        if type(self.max_instances) is not int or self.max_instances < 1:
            raise ValueError("max_instances must be a positive integer")
        if not math.isfinite(self.idle_timeout) or self.idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive and finite")
        if not math.isfinite(self.cf_wait_seconds) or self.cf_wait_seconds < 0:
            raise ValueError("cf_wait_seconds must be nonnegative and finite")
        if not math.isfinite(self.page_settle_seconds) or self.page_settle_seconds < 0:
            raise ValueError("page_settle_seconds must be nonnegative and finite")
        if len({account.id.casefold() for account in self.accounts}) != len(self.accounts):
            raise ValueError("Duplicate browser account ID")

    @classmethod
    def from_toml(cls, path: Path, signing_key: str | None = None) -> "BrowserConfig":
        try:
            with path.open("rb") as source:
                data = tomllib.load(source)
            if signing_key is not None:
                data["signing_key"] = signing_key
            for name in ("binary_path", "profiles_root"):
                data[name] = path.parent / Path(data[name]).expanduser()
            accounts_file = path.parent / Path(data.pop("accounts_file", "accounts.local.toml")).expanduser()
            managed = []
            if accounts_file.exists():
                with accounts_file.open("rb") as source:
                    managed = tomllib.load(source).get("accounts", [])
            data["accounts"] = tuple(AccountConfig(**account) for account in data.get("accounts", []) + managed)
            data["accounts_file"] = accounts_file
            data["managed_ids"] = frozenset(account["id"] for account in managed)
            return cls(**data)
        except (OSError, KeyError, TypeError, ValueError, OverflowError):
            pass
        raise ValueError("Cannot read browser configuration")


def write_accounts(path: Path, accounts: list[AccountConfig]) -> None:
    lines = []
    for account in accounts:
        lines.append("[[accounts]]")
        for key in ("id", "proxy_url", "fingerprint_seed", "timezone", "persona", "disable_spoofing", "email", "password"):
            value = getattr(account, key)
            if value is None:
                continue
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=True)}")
        lines.append("")
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".accounts-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write("\n".join(lines))
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)
