from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def safe_ref(value: str) -> str:
    if ".." in value:
        raise ValueError("Invalid reference")
    return value


Ref = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_@+.-]{0,127}$"), AfterValidator(safe_ref)]
CoinRef = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_@+.~:-]*$")]
CatalogSymbol = Annotated[str, Field(min_length=1)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def reject_bool(value):
    if isinstance(value, bool):
        raise ValueError("balance_min must be a non-negative integer")
    return value


BalanceMin = Annotated[int, Field(ge=0), BeforeValidator(reject_bool)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Interval = Literal["7d", "14d", "30d"]
TransferDirection = Literal["inbound", "outbound", "internal", "entity_to_entity", "unknown"]
Level = Literal["important", "normal", "log"]
UTCDateTime = Annotated[datetime, AfterValidator(
    lambda value: value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
)]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


CoinStatus = Literal["supported", "no_match", "ambiguous", "unsupported", "needs_manual_mapping",
                     "unknown_onchain", "excluded", "no_futures", "pending_match", "pending_support", "match_failed"]
Feed = Literal["spot", "alpha"]


class Deployment(Schema):
    feed: Feed
    chain: str
    address: str | None
    deposit_enabled: bool | None
    withdraw_enabled: bool | None

    @model_validator(mode="before")
    @classmethod
    def public_fields(cls, value):
        if isinstance(value, dict):
            return {key: value[key] for key in cls.model_fields if key in value}
        return value


class CoinOut(Schema):
    id: CoinRef
    symbol: str
    pricing_id: Ref | None
    status: CoinStatus
    reason: str | None
    sources: list[Feed]
    deployments: list[Deployment]
    matched_chains: list[str]
    match_failures: int
    last_match_attempt: float | None


class CatalogSettingsInput(Schema):
    failure_limit: Annotated[int, Field(gt=0, strict=True)]
    refresh_retry_seconds: Annotated[int, Field(gt=0, strict=True)]


class CatalogStatus(Schema):
    status: Literal["validated", "unknown"]
    attempted_at: float | None
    refreshed_at: float | None
    error: str | None


class RematchInput(Schema):
    statuses: list[Literal["no_match"]] = Field(default_factory=lambda: ["no_match"])


class RematchResult(Schema):
    count: int


class FuturesMappingInput(Schema):
    symbol: CatalogSymbol


class FuturesMappingOut(FuturesMappingInput):
    base: CatalogSymbol


class ChainObservationOut(Schema):
    alpha_chain_id: str
    arkham_chains: list[str]
    conflict: bool


class MappingInput(Schema):
    pricing_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"), AfterValidator(safe_ref)]


class SubscriptionInput(Schema):
    enabled: bool = True


class SubscriptionOut(SubscriptionInput):
    coin_id: str


class AccountInput(Schema):
    status: Literal["active", "paused"] | None = None
    proxy_url: str | None = None
    password: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class AccountCreate(Schema):
    email: Annotated[str, Field(max_length=128, pattern=r"^[a-z0-9][a-z0-9._+-]*@[a-z0-9-]+(\.[a-z0-9-]+)+$")]
    password: Annotated[str, Field(min_length=1, max_length=256)]
    proxy_url: str

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        return safe_ref(value)


class AccountOut(Schema):
    id: str
    proxy_ref: str
    status: Literal["active", "paused"]
    pause_reason: str | None
    available_at: float
    failures: int
    session_status: Literal["logged_in", "logged_out", "unknown"] | None = None
    session_checked_at: float | None = None
    managed: bool = False
    timezone: str | None = None
    persona: str | None = None
    email: str | None = None
    password: str | None = None
    proxy_url: str | None = None


class AccountSession(Schema):
    status: Literal["logged_in", "logged_out", "unknown"]


class LoginSession(Schema):
    state: Literal["none", "starting", "open", "closed", "failed"]
    path: str | None


class LoginInput(Schema):
    page: Literal["login", "register"] = "login"


class ExclusionInput(Schema):
    reason: Annotated[str, Field(min_length=1)]


class ExclusionOut(ExclusionInput):
    coin_id: CoinRef


PolicyKind = Literal["flag", "tag"]


class CatalogExclusionOut(ExclusionInput):
    kind: PolicyKind
    value: CatalogSymbol


class JobOptions(Schema):
    balance_min: BalanceMin | None = None


class JobInput(Schema):
    kind: Literal["transfers", "daily", "coin_catalog"]
    account_id: Ref | None = None
    coin_id: CoinRef | None = None
    options: JobOptions = Field(default_factory=JobOptions)
    interval_seconds: Positive
    enabled: bool = False

    @model_validator(mode="after")
    def validate_parameters(self):
        if (self.kind == "transfers") != (self.coin_id is not None):
            raise ValueError("only transfer jobs require a coin")
        if (self.kind == "daily") != (self.options.balance_min is not None):
            raise ValueError("only daily jobs require balance_min")
        return self


class JobOut(JobInput):
    id: int
    next_run: float
    wait_reason: str | None
    last_error: str | None


class RuleConditions(Schema):
    coins_include: list[CoinRef] | None = None
    coins_exclude: list[CoinRef] | None = None
    entities_include: list[str] | None = None
    entities_exclude: list[str] | None = None
    direction: TransferDirection | Literal["increase", "decrease"] | None = None
    min_usd: NonNegative | None = None
    min_balance_change_pct: NonNegative | None = None


class RuleInput(Schema):
    name: Annotated[str, Field(min_length=1)]
    enabled: bool
    source: Literal["transfer", "balance_diff"]
    conditions: RuleConditions
    level: Level
    push: bool
    cooldown_seconds: NonNegative | None = None
    merge_seconds: NonNegative | None = None

    @model_validator(mode="after")
    def validate_source(self):
        direction = self.conditions.direction
        if direction and ((direction in {"increase", "decrease"}) != (self.source == "balance_diff")):
            raise ValueError("direction does not match trigger source")
        if self.source == "transfer" and self.conditions.min_balance_change_pct is not None:
            raise ValueError("balance change threshold requires a balance_diff rule")
        return self


class RuleOut(RuleInput):
    id: int


class TransferOut(Schema):
    id: str
    coin_ids: list[str]
    txhash: str | None
    chain: str | None
    from_address: str | None
    to_address: str | None
    from_entity: str | None
    to_entity: str | None
    direction: TransferDirection
    usd: float | None
    time: UTCDateTime
    gap: bool


class EventOut(Schema):
    id: int
    kind: str
    rule_id: int | None
    coin_id: str | None
    level: Level
    push: bool
    created_at: UTCDateTime
    last_at: UTCDateTime
    count: int
    read_at: UTCDateTime | None
    data: dict


class ReadInput(Schema):
    read: bool


class BalancePoint(Schema):
    day: date
    entity_id: str
    entity_name: str | None
    entity_type: str | None
    balance_unit: float
    balance_usd: float


class BalanceDay(Schema):
    day: date
    truncated: bool


class BalanceCurve(Schema):
    coin_id: str
    days: list[BalanceDay]
    series: dict[str, list[BalancePoint]]


class SnapshotInput(Schema):
    account_id: Ref
    interval: Interval = "7d"
    balance_min: BalanceMin | None = None
    pricing_ids: Annotated[list[Ref], Field(min_length=1, max_length=50)] | None = None


class SnapshotOut(Schema):
    id: int
    created_at: UTCDateTime
    interval: Interval
    balance_min: float | None
    pricing_ids: list[str] | None
    truncated: bool


class SnapshotRowOut(Schema):
    entity_id: str
    entity_name: str | None
    entity_type: str | None
    balance_unit: float | None
    balance_usd: float | None
    prev_balance_unit: float | None
    prev_balance_usd: float | None
    token_balances: list | dict | None


class SnapshotDetail(SnapshotOut):
    rows: list[SnapshotRowOut]


class ChangedRow(Schema):
    before: SnapshotRowOut
    after: SnapshotRowOut


class SnapshotDiff(Schema):
    added: list[SnapshotRowOut]
    removed: list[SnapshotRowOut]
    changed: list[ChangedRow]


class EntityBalanceOut(Schema):
    entity_id: str
    entity_name: str | None
    entity_type: str | None
    balance_unit: float
    balance_usd: float
    day: date
