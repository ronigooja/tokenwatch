from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


Ref = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Interval = Literal["7d", "14d", "30d"]
TransferDirection = Literal["inbound", "outbound", "internal", "entity_to_entity", "unknown"]
Level = Literal["important", "normal", "log"]
UTCDateTime = Annotated[datetime, AfterValidator(
    lambda value: value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
)]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CoinInput(Schema):
    symbol: Ref
    pricing_id: Ref | None = None
    verified: bool = False

    @model_validator(mode="after")
    def validate_pricing_id(self):
        if self.verified and not self.pricing_id:
            raise ValueError("verified coins need a pricing ID")
        return self


class CoinOut(CoinInput):
    id: str
    exclusion_reason: str | None
    sources: list[dict]


class MappingInput(Schema):
    pricing_id: Ref


class SubscriptionInput(Schema):
    enabled: bool = True


class SubscriptionOut(SubscriptionInput):
    coin_id: str


class AccountInput(Schema):
    proxy_ref: Ref
    status: Literal["active", "paused"] = "active"


class AccountCreate(Schema):
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")]
    proxy_url: str
    timezone: str
    persona: Literal["none", "windows"] = "windows"


class AccountOut(AccountInput):
    id: str
    pause_reason: str | None
    available_at: float
    failures: int
    managed: bool = False
    timezone: str | None = None
    persona: str | None = None


class AccountSession(Schema):
    status: Literal["logged_in", "logged_out", "unknown"]


class ExclusionInput(Schema):
    reason: Annotated[str, Field(min_length=1)]


class ExclusionOut(ExclusionInput):
    coin_id: str


class JobOptions(Schema):
    balance_min: NonNegative | None = None


class JobInput(Schema):
    kind: Literal["transfers", "daily", "coin_catalog"]
    account_id: Ref
    coin_id: Ref | None = None
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


class RuleConditions(Schema):
    coins_include: list[Ref] | None = None
    coins_exclude: list[Ref] | None = None
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
    balance_min: NonNegative | None = None
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
