from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Index, UniqueConstraint, URL, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Coin(Base):
    __tablename__ = "coins"
    id: Mapped[str] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(index=True)
    pricing_id: Mapped[str | None]
    verified: Mapped[bool] = mapped_column(default=False)
    exclusion_reason: Mapped[str | None]
    sources: Mapped[list] = mapped_column(default=list)


class CoinMapping(Base):
    __tablename__ = "coin_mappings"
    coin_id: Mapped[str] = mapped_column(primary_key=True)
    pricing_id: Mapped[str]


class CatalogCache(Base):
    __tablename__ = "catalog_cache"
    key: Mapped[str] = mapped_column(primary_key=True)
    payload: Mapped[list] = mapped_column(default=list)
    refreshed_at: Mapped[float] = mapped_column(default=0)
    attempted_at: Mapped[float] = mapped_column(default=0)


class CatalogMapping(Base):
    __tablename__ = "catalog_mappings"
    symbol: Mapped[str] = mapped_column(primary_key=True)
    pricing_id: Mapped[str | None]
    ambiguous: Mapped[bool]


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(primary_key=True)
    proxy_ref: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="active")
    pause_reason: Mapped[str | None]
    available_at: Mapped[float] = mapped_column(default=0)
    failures: Mapped[int] = mapped_column(default=0)


class Subscription(Base):
    __tablename__ = "coin_subscriptions"
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    coin_id: Mapped[str | None] = mapped_column(ForeignKey("coins.id"))
    options: Mapped[dict] = mapped_column(default=dict)
    interval_seconds: Mapped[float]
    enabled: Mapped[bool] = mapped_column(default=False)
    next_run: Mapped[float] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("kind", "coin_id"),
                      Index("ix_jobs_singleton_kind", "kind", unique=True, sqlite_where=text("coin_id IS NULL")),
                      {"sqlite_autoincrement": True})


class Exclusion(Base):
    __tablename__ = "exclusions"
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id"), primary_key=True)
    reason: Mapped[str]


class DailyCollection(Base):
    __tablename__ = "daily_collections"
    day: Mapped[date] = mapped_column("date", primary_key=True)
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id"), primary_key=True)
    truncated: Mapped[bool]


class DailyBalance(Base):
    __tablename__ = "daily_entity_balances"
    day: Mapped[date] = mapped_column("date", primary_key=True)
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id"), primary_key=True)
    entity_id: Mapped[str] = mapped_column(primary_key=True)
    entity_name: Mapped[str | None]
    entity_type: Mapped[str | None]
    balance_unit: Mapped[float]
    balance_usd: Mapped[float]
    __table_args__ = (Index("ix_balance_curve", "coin_id", "entity_id", "date"),)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    interval: Mapped[str]
    balance_min: Mapped[float | None]
    pricing_ids: Mapped[list | None]
    truncated: Mapped[bool]
    __table_args__ = {"sqlite_autoincrement": True}


class SnapshotRow(Base):
    __tablename__ = "snapshot_rows"
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), primary_key=True)
    entity_id: Mapped[str] = mapped_column(primary_key=True)
    entity_name: Mapped[str | None]
    entity_type: Mapped[str | None]
    balance_unit: Mapped[float | None]
    balance_usd: Mapped[float | None]
    prev_balance_unit: Mapped[float | None]
    prev_balance_usd: Mapped[float | None]
    token_balances: Mapped[list | dict | None] = mapped_column(JSON)


class Transfer(Base):
    __tablename__ = "transfers"
    id: Mapped[str] = mapped_column(primary_key=True)
    txhash: Mapped[str | None] = mapped_column(index=True)
    chain: Mapped[str | None] = mapped_column(index=True)
    from_address: Mapped[str | None]
    to_address: Mapped[str | None]
    from_entity: Mapped[str | None]
    to_entity: Mapped[str | None]
    direction: Mapped[str]
    usd: Mapped[float | None]
    time: Mapped[datetime] = mapped_column(index=True)
    gap: Mapped[bool]


class TransferCoin(Base):
    __tablename__ = "transfer_coins"
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.id", ondelete="CASCADE"), primary_key=True)
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id"), primary_key=True, index=True)


class TransferCursor(Base):
    __tablename__ = "transfer_cursors"
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id", ondelete="CASCADE"), primary_key=True)
    pricing_id: Mapped[str]
    ids: Mapped[list]
    last_poll: Mapped[float]
    rate: Mapped[float | None]
    interval: Mapped[float]


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    enabled: Mapped[bool]
    source: Mapped[str]
    conditions: Mapped[dict]
    level: Mapped[str]
    push: Mapped[bool]
    cooldown_seconds: Mapped[float | None]
    merge_seconds: Mapped[float | None]
    __table_args__ = {"sqlite_autoincrement": True}


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[str] = mapped_column(index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id", ondelete="SET NULL"))
    coin_id: Mapped[str | None] = mapped_column(ForeignKey("coins.id", ondelete="SET NULL"), index=True)
    group_key: Mapped[str | None] = mapped_column(index=True)
    level: Mapped[str] = mapped_column(index=True)
    push: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(default=now)
    last_at: Mapped[datetime] = mapped_column(default=now)
    count: Mapped[int] = mapped_column(default=1)
    read_at: Mapped[datetime | None]
    data: Mapped[dict]
    __table_args__ = {"sqlite_autoincrement": True}


class RuleMatch(Base):
    __tablename__ = "rule_matches"
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id", ondelete="CASCADE"), primary_key=True)
    source_key: Mapped[str] = mapped_column(primary_key=True)


class PushLog(Base):
    __tablename__ = "push_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    sent_at: Mapped[datetime] = mapped_column(default=now)


def open_store(path: Path):
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)), connect_args={"timeout": 5})

    @event.listens_for(engine, "connect")
    def set_pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")

    with engine.connect() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
        connection.execute(text("BEGIN IMMEDIATE"))
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        if version == 0:
            Base.metadata.create_all(connection)
            connection.execute(text("PRAGMA user_version=1"))
        elif version != 1:
            raise RuntimeError(f"Unsupported database schema version: {version}")
        CatalogCache.__table__.create(connection, checkfirst=True)
        CatalogMapping.__table__.create(connection, checkfirst=True)
        connection.commit()
    return sessionmaker(engine, expire_on_commit=False)
