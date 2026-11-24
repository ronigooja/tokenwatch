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
    status: Mapped[str] = mapped_column(default="unknown_onchain", index=True)
    reason: Mapped[str | None]
    sources: Mapped[list] = mapped_column(default=list)
    deployments: Mapped[list] = mapped_column(default=list)
    searched_deployments: Mapped[list] = mapped_column(default=list)
    matched_chains: Mapped[list] = mapped_column(default=list)
    # Keep catalog eligibility and Arkham outcomes separate so reclassification never
    # causes another request for an already resolved coin.
    catalog_status: Mapped[str | None]
    catalog_reason: Mapped[str | None]
    match_status: Mapped[str | None]
    match_reason: Mapped[str | None]
    automatic_pricing_id: Mapped[str | None]
    automatic_chains: Mapped[list] = mapped_column(default=list)
    manual_pricing_id: Mapped[str | None]
    manual_status: Mapped[str | None]
    match_failures: Mapped[int] = mapped_column(default=0, server_default="0")
    last_match_attempt: Mapped[float | None]


class CatalogSettings(Base):
    __tablename__ = "catalog_settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    failure_limit: Mapped[int]
    refresh_retry_seconds: Mapped[int | None]


class CoinMapping(Base):
    __tablename__ = "coin_mappings"
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id"), primary_key=True)
    pricing_id: Mapped[str]


class CatalogCache(Base):
    __tablename__ = "catalog_cache"
    key: Mapped[str] = mapped_column(primary_key=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
    refreshed_at: Mapped[float | None]
    attempted_at: Mapped[float]
    error: Mapped[str | None]


class FuturesMapping(Base):
    __tablename__ = "futures_mappings"
    base: Mapped[str] = mapped_column(primary_key=True)
    symbol: Mapped[str]


class CatalogExclusion(Base):
    __tablename__ = "catalog_exclusions"
    kind: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(primary_key=True)
    reason: Mapped[str]


class ChainObservation(Base):
    __tablename__ = "alpha_chain_observations"
    alpha_chain_id: Mapped[str] = mapped_column(primary_key=True)
    arkham_chain: Mapped[str] = mapped_column(primary_key=True)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(primary_key=True)
    proxy_ref: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="active")
    pause_reason: Mapped[str | None]
    available_at: Mapped[float] = mapped_column(default=0)
    failures: Mapped[int] = mapped_column(default=0)
    last_used_at: Mapped[float] = mapped_column(default=0)


class Subscription(Base):
    __tablename__ = "coin_subscriptions"
    coin_id: Mapped[str] = mapped_column(ForeignKey("coins.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    # A disabled legacy transfer job retains its old selector until reassigned.
    coin_id: Mapped[str | None]
    options: Mapped[dict] = mapped_column(default=dict)
    interval_seconds: Mapped[float]
    enabled: Mapped[bool] = mapped_column(default=False)
    next_run: Mapped[float] = mapped_column(default=0)
    wait_reason: Mapped[str | None]
    last_error: Mapped[str | None]
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
    coin_id: Mapped[str] = mapped_column(primary_key=True, index=True)


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
    coin_id: Mapped[str | None] = mapped_column(index=True)
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


def migrate_v1(connection):
    execute = connection.exec_driver_sql
    execute("ALTER TABLE accounts ADD COLUMN last_used_at FLOAT NOT NULL DEFAULT 0")
    execute("CREATE TEMP TABLE saved_jobs AS SELECT * FROM jobs")
    execute("CREATE TEMP TABLE saved_job_seq AS SELECT seq FROM sqlite_sequence WHERE name='jobs'")
    execute("CREATE TEMP TABLE saved_transfer_coins AS SELECT * FROM transfer_coins")
    execute("CREATE TEMP TABLE saved_events AS SELECT * FROM events")
    execute("CREATE TEMP TABLE saved_event_seq AS SELECT seq FROM sqlite_sequence WHERE name='events'")
    for table in ("jobs", "coin_subscriptions", "exclusions", "daily_entity_balances", "daily_collections",
                  "transfer_coins", "transfer_cursors", "coin_mappings", "catalog_mappings", "catalog_cache", "events", "coins"):
        execute(f"DROP TABLE IF EXISTS {table}")
    Base.metadata.create_all(connection)
    execute("""INSERT INTO jobs (id, kind, account_id, coin_id, options, interval_seconds, enabled, next_run, wait_reason)
        SELECT id, kind, account_id, coin_id, options, interval_seconds,
            CASE WHEN kind='transfers' THEN 0 ELSE enabled END, next_run,
            CASE WHEN kind='transfers' THEN 'coin_reassignment_required' END FROM saved_jobs""")
    execute("INSERT INTO events SELECT * FROM saved_events")
    for name, saved in (("jobs", "saved_job_seq"), ("events", "saved_event_seq")):
        execute(f"INSERT INTO sqlite_sequence (name, seq) SELECT '{name}', seq FROM {saved} "
                f"WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='{name}')")
        execute(f"UPDATE sqlite_sequence SET seq=MAX(seq, COALESCE((SELECT seq FROM {saved}), 0)) WHERE name='{name}'")
    execute("INSERT INTO transfer_coins SELECT * FROM saved_transfer_coins")
    for table in ("saved_jobs", "saved_job_seq", "saved_transfer_coins", "saved_events", "saved_event_seq"):
        execute(f"DROP TABLE {table}")


def open_store(path: Path):
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)), connect_args={"timeout": 5})

    @event.listens_for(engine, "connect")
    def set_pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")

    with engine.connect() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text("BEGIN IMMEDIATE"))
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        if version == 0:
            Base.metadata.create_all(connection)
        elif version == 1:
            migrate_v1(connection)
        elif version not in (2, 3, 4, 5, 6):
            raise RuntimeError(f"Unsupported database schema version: {version}")
        if version == 2:
            connection.exec_driver_sql("ALTER TABLE jobs ADD COLUMN last_error VARCHAR")
        if version in (2, 3):
            connection.exec_driver_sql("ALTER TABLE coins ADD COLUMN match_failures INTEGER NOT NULL DEFAULT 0")
            connection.exec_driver_sql("ALTER TABLE coins ADD COLUMN last_match_attempt FLOAT")
            CatalogSettings.__table__.create(connection, checkfirst=True)
        if version in (2, 3, 4):
            connection.exec_driver_sql("ALTER TABLE coins ADD COLUMN searched_deployments JSON NOT NULL DEFAULT '[]'")
        if version in (2, 3, 4, 5) and "refresh_retry_seconds" not in {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(catalog_settings)")
        }:
            connection.exec_driver_sql("ALTER TABLE catalog_settings ADD COLUMN refresh_retry_seconds INTEGER")
        if version == 1 and connection.exec_driver_sql("PRAGMA foreign_key_check").first():
            raise RuntimeError("Foreign key violation after coin schema migration")
        connection.execute(text("PRAGMA user_version=6"))
        connection.commit()
        connection.execute(text("PRAGMA foreign_keys=ON"))
    return sessionmaker(engine, expire_on_commit=False)
