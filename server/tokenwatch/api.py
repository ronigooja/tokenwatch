import asyncio
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from importlib import import_module
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import schemas as s
from .coins import CoinCatalog, sync_coin
from .collectors import Collectors, snapshot_diff
from .config import load_settings
from .login_session import LoginSessions
from . import novnc
from .db import (Account, CatalogCache, CatalogExclusion, ChainObservation, Coin, CoinMapping, DailyBalance,
                 DailyCollection, Event, Exclusion, FuturesMapping, Job, PushLog, Rule, Snapshot,
                 SnapshotRow, Subscription, Transfer, TransferCoin, now, open_store)
from .scheduler import AccountUnavailable, RequestGate, Scheduler
from .upstream import UpstreamError
from .browser.config import AccountConfig, proxy_parts
from .browser.timezone import detect_timezone


Page = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]
bearer = HTTPBearer(auto_error=False)


def token_matches(value, settings):
    return bool(value) and secrets.compare_digest(value.encode(), settings.bearer_token.get_secret_value().encode())


def authorize(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not credentials or not token_matches(credentials.credentials, request.app.state.settings):
        raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})


def db_session(request: Request):
    with request.app.state.store.begin() as db:
        yield db


DB = Annotated[Session, Depends(db_session, scope="function")]


def find(db, model, key):
    result = db.get(model, key)
    if result is None:
        raise HTTPException(404, "Resource not found")
    return result


def utc_naive(stamp):
    return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp


def add_bounds(query, column, lower, upper):
    if lower is not None and upper is not None and lower > upper:
        raise HTTPException(422, "Lower bound exceeds upper bound")
    if lower is not None:
        query = query.where(column >= lower)
    if upper is not None:
        query = query.where(column <= upper)
    return query


def resource(app, path, model, request_model, response_model, *, key="id", key_type=s.Ref,
             generated=False, validate=None, remove=None):
    route = f"/{path}"
    item_route = f"{route}/{{resource_id}}"
    auth = [Depends(authorize)]

    def list_items(db: DB):
        return db.scalars(select(model).order_by(getattr(model, key))).all()

    def get_item(resource_id: key_type, db: DB):
        return find(db, model, resource_id)

    def save(resource_id: key_type, body: request_model, db: DB):
        current = find(db, model, resource_id) if generated else db.get(model, resource_id)
        if not current:
            current = model(**{key: resource_id})
        values = body.model_dump()
        if validate:
            validate(db, current, values)
        for field, value in values.items():
            setattr(current, field, value)
        db.add(current)
        db.flush()
        return current

    def create(body: request_model, db: DB):
        current = model()
        values = body.model_dump()
        if validate:
            validate(db, current, values)
        for field, value in values.items():
            setattr(current, field, value)
        db.add(current)
        db.flush()
        return current

    def delete_item(resource_id: key_type, db: DB):
        current = find(db, model, resource_id)
        if remove:
            remove(db, current)
        db.delete(current)
        db.flush()
        return Response(status_code=204)

    def add(url, name, endpoint, method, schema=None, code=200):
        app.add_api_route(url, endpoint, methods=[method], response_model=schema, status_code=code,
                          dependencies=auth, operation_id=f"{name}_{path}")

    add(route, "list", list_items, "GET", list[response_model])
    add(item_route, "get", get_item, "GET", response_model)
    add(item_route, "put", save, "PUT", response_model)
    add(item_route, "delete", delete_item, "DELETE", code=204)
    if generated:
        add(route, "create", create, "POST", response_model, 201)


def validate_account(db, current, values):
    if values["status"] == "active":
        values["pause_reason"] = None
    elif current.status != "paused":
        values["pause_reason"] = "manual"


def validate_job(db, current, values):
    if values["account_id"] is not None:
        find(db, Account, values["account_id"])
    if values["coin_id"]:
        find(db, Coin, values["coin_id"])
    values["next_run"] = 0
    values["wait_reason"] = None


def validate_subscription(db, current, values):
    find(db, Coin, current.coin_id)


def reclassify_catalog(db):
    cache = db.get(CatalogCache, "binance")
    if cache:
        CoinCatalog.reclassify(db, cache.payload)


def create_app(settings=None, upstream=None, *, catalog_transport=None, run_scheduler=True,
               timezone_detector=detect_timezone):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or load_settings()
        client = upstream
        if client is None and config.upstream_factory:
            module, separator, factory = config.upstream_factory.partition(":")
            if not separator:
                raise ValueError("upstream_factory must be module:factory")
            client = getattr(import_module(module), factory)(config)
        store = open_store(config.database)
        app.state.settings, app.state.store, app.state.upstream = config, store, client
        app.state.accounts_lock = asyncio.Lock()
        app.state.timezone_detector = timezone_detector
        app.state.collectors = None
        app.state.gate = None
        task = None
        try:
            if client is not None:
                gate = RequestGate(store, client, config)
                app.state.gate = gate
                catalog = CoinCatalog(store, gate, transport=catalog_transport)
                collectors = Collectors(store, gate, config, catalog=catalog)
                app.state.collectors = collectors
                if run_scheduler:
                    task = asyncio.create_task(Scheduler(store, config, {
                        "daily": collectors.daily, "transfers": collectors.transfers, "coin_catalog": catalog.refresh,
                    }).run())
            app.state.logins = LoginSessions(store, client, app.state.gate, config)
            yield
        finally:
            if hasattr(app.state, "logins"):
                await app.state.logins.shutdown()
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            store.kw["bind"].dispose()

    app = FastAPI(title="Tokenwatch", version="0.1.0", lifespan=lifespan)
    auth = [Depends(authorize)]
    novnc.install(app)

    @app.post("/accounts/{account_id}/login", response_model=s.LoginSession, status_code=201, dependencies=auth)
    async def start_login(account_id: s.Ref, request: Request, body: s.LoginInput | None = None):
        return await request.app.state.logins.start(account_id, page=body.page if body else "login")

    @app.get("/accounts/{account_id}/login", response_model=s.LoginSession, dependencies=auth)
    async def get_login(account_id: s.Ref, request: Request, db: DB):
        find(db, Account, account_id)
        return request.app.state.logins.view(account_id)

    @app.post("/accounts/{account_id}/login/finish", response_model=s.AccountSession, dependencies=auth)
    async def finish_login(account_id: s.Ref, request: Request):
        return await request.app.state.logins.finish(account_id)

    @app.exception_handler(IntegrityError)
    async def conflict(_request, _error):
        return JSONResponse(status_code=409, content={"detail": "Resource conflicts with existing data"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if (request.url.path == "/accounts" and request.method == "POST" or
                request.url.path.startswith("/accounts/") and request.method == "PUT"):
            return JSONResponse(status_code=422, content={"detail": "Invalid account input"})
        from fastapi.encoders import jsonable_encoder
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(error.errors())})

    resource(app, "subscriptions", Subscription, s.SubscriptionInput, s.SubscriptionOut,
             key="coin_id", key_type=s.CoinRef, validate=validate_subscription)
    resource(app, "jobs", Job, s.JobInput, s.JobOut, generated=True, key_type=int, validate=validate_job)
    resource(app, "rules", Rule, s.RuleInput, s.RuleOut, generated=True, key_type=int)

    @app.get("/coins", response_model=list[s.CoinOut], dependencies=auth, operation_id="list_coins")
    def list_coins(db: DB, status: s.CoinStatus | None = None):
        query = select(Coin).order_by(Coin.symbol, Coin.id)
        return db.scalars(query.where(Coin.status == status) if status else query).all()

    @app.get("/coins/{coin_id}", response_model=s.CoinOut, dependencies=auth, operation_id="get_coins")
    def get_coin(coin_id: s.CoinRef, db: DB):
        return find(db, Coin, coin_id)

    @app.get("/coin-catalog", response_model=s.CatalogStatus, dependencies=auth)
    def get_catalog_status(db: DB):
        cache = db.get(CatalogCache, "binance")
        return {"status": "validated" if cache and not cache.error else "unknown",
                "attempted_at": cache.attempted_at if cache else None,
                "refreshed_at": cache.refreshed_at if cache else None,
                "error": cache.error if cache else None}

    @app.get("/coin-catalog/chains", response_model=list[s.ChainObservationOut], dependencies=auth)
    def list_chain_observations(db: DB):
        observed = defaultdict(list)
        for row in db.scalars(select(ChainObservation).order_by(ChainObservation.alpha_chain_id, ChainObservation.arkham_chain)):
            observed[row.alpha_chain_id].append(row.arkham_chain)
        return [{"alpha_chain_id": chain, "arkham_chains": names, "conflict": len(names) > 1}
                for chain, names in observed.items()]

    @app.get("/coin-catalog/futures-mappings", response_model=list[s.FuturesMappingOut], dependencies=auth)
    def list_futures_mappings(db: DB):
        return db.scalars(select(FuturesMapping).order_by(FuturesMapping.base)).all()

    @app.put("/coin-catalog/futures-mappings/{base}", response_model=s.FuturesMappingOut, dependencies=auth)
    def put_futures_mapping(base: s.CatalogSymbol, body: s.FuturesMappingInput, db: DB):
        row = db.merge(FuturesMapping(base=base.upper(), symbol=body.symbol.upper()))
        db.flush()
        reclassify_catalog(db)
        return row

    @app.delete("/coin-catalog/futures-mappings/{base}", status_code=204, dependencies=auth)
    def delete_futures_mapping(base: s.CatalogSymbol, db: DB):
        db.delete(find(db, FuturesMapping, base.upper()))
        db.flush()
        reclassify_catalog(db)
        return Response(status_code=204)

    @app.get("/coin-catalog/exclusion-rules", response_model=list[s.CatalogExclusionOut], dependencies=auth)
    def list_catalog_exclusions(db: DB):
        return db.scalars(select(CatalogExclusion).order_by(CatalogExclusion.kind, CatalogExclusion.value)).all()

    @app.put("/coin-catalog/exclusion-rules/{kind}/{value}", response_model=s.CatalogExclusionOut, dependencies=auth)
    def put_catalog_exclusion(kind: s.PolicyKind, value: s.CatalogSymbol, body: s.ExclusionInput, db: DB):
        if kind == "flag" and value != "isLegalMoney":
            raise HTTPException(422, "Unknown Binance asset flag")
        row = db.merge(CatalogExclusion(kind=kind, value=value, reason=body.reason))
        db.flush()
        reclassify_catalog(db)
        return row

    @app.delete("/coin-catalog/exclusion-rules/{kind}/{value}", status_code=204, dependencies=auth)
    def delete_catalog_exclusion(kind: s.PolicyKind, value: s.CatalogSymbol, db: DB):
        db.delete(find(db, CatalogExclusion, (kind, value)))
        db.flush()
        reclassify_catalog(db)
        return Response(status_code=204)

    @app.get("/exclusions", response_model=list[s.ExclusionOut], dependencies=auth, operation_id="list_exclusions")
    def list_exclusions(db: DB):
        query = select(Coin).where(Coin.status == "excluded").order_by(Coin.id)
        return [{"coin_id": row.id, "reason": row.reason} for row in db.scalars(query)]

    @app.get("/exclusions/{coin_id}", response_model=s.ExclusionOut, dependencies=auth)
    def get_exclusion(coin_id: s.CoinRef, db: DB):
        coin = find(db, Coin, coin_id)
        if coin.status != "excluded":
            raise HTTPException(404, "Resource not found")
        return {"coin_id": coin_id, "reason": coin.reason}

    @app.put("/exclusions/{coin_id}", response_model=s.ExclusionOut, dependencies=auth)
    def put_exclusion(coin_id: s.CoinRef, body: s.ExclusionInput, db: DB):
        coin = find(db, Coin, coin_id)
        row = db.merge(Exclusion(coin_id=coin_id, reason=body.reason))
        db.flush()
        sync_coin(db, coin)
        return row

    @app.delete("/exclusions/{coin_id}", status_code=204, dependencies=auth)
    def delete_exclusion(coin_id: s.CoinRef, db: DB):
        db.delete(find(db, Exclusion, coin_id))
        db.flush()
        sync_coin(db, find(db, Coin, coin_id))
        return Response(status_code=204)

    def account_out(row, client):
        getter = getattr(client, "account_config", None)
        config, managed = getter(row.id) if getter else (None, False)
        gate = app.state.gate
        checked_at, status = gate.status_cache.get(row.id, (None, None)) if gate else (None, None)
        return {**s.AccountOut.model_validate(row).model_dump(), "managed": managed,
                "session_status": status, "session_checked_at": checked_at,
                "timezone": config.timezone if config else None, "persona": config.persona if config else None,
                "email": config.email if managed else None, "password": config.password if managed else None,
                "proxy_url": config.proxy_url if managed else None}

    async def timezone_for(account):
        try:
            zone = await app.state.timezone_detector(account)
            ZoneInfo(zone)
            return zone
        except Exception:
            raise HTTPException(502, "Cannot determine proxy timezone") from None

    @app.get("/accounts", response_model=list[s.AccountOut], dependencies=auth, operation_id="list_accounts")
    def list_accounts(db: DB, request: Request):
        return [account_out(row, request.app.state.upstream) for row in db.scalars(select(Account).order_by(Account.id))]

    @app.get("/accounts/{account_id}", response_model=s.AccountOut, dependencies=auth, operation_id="get_accounts")
    def get_account(account_id: s.Ref, db: DB, request: Request):
        return account_out(find(db, Account, account_id), request.app.state.upstream)

    @app.post("/accounts", response_model=s.AccountOut, status_code=201, dependencies=auth, operation_id="create_accounts")
    async def create_account(body: s.AccountCreate, request: Request):
        client = request.app.state.upstream
        if not callable(getattr(client, "add_account", None)):
            raise HTTPException(503, "Browser pool unavailable")
        try:
            proxy_parts(body.proxy_url)
        except ValueError:
            raise HTTPException(422, "Invalid account input") from None
        async with request.app.state.accounts_lock:
            with request.app.state.store.begin() as db:
                rows = db.scalars(select(Account)).all()
                proxy_ref = f"proxy-{body.email}"
                if any(row.id.casefold() == body.email.casefold() or row.proxy_ref == proxy_ref for row in rows):
                    raise HTTPException(409, "Account conflicts with existing data")
                used = {a.fingerprint_seed for a in getattr(client, "_accounts", {}).values()}
                seed = secrets.randbits(32)
                while seed in used:
                    seed = secrets.randbits(32)
                account = AccountConfig(body.email, body.proxy_url, seed, persona="windows",
                                        email=body.email, password=body.password)
                account = replace(account, timezone=await timezone_for(account))
                row = Account(id=body.email, proxy_ref=proxy_ref, status="active")
                db.add(row)
                db.flush()
                try:
                    await client.add_account(account)
                except ValueError:
                    raise HTTPException(409, "Account conflicts with existing data") from None
                except (OSError, UpstreamError):
                    raise HTTPException(503, "Browser pool unavailable") from None
                return account_out(row, client)

    @app.put("/accounts/{account_id}", response_model=s.AccountOut, dependencies=auth, operation_id="put_accounts")
    async def put_account(account_id: s.Ref, body: s.AccountInput, request: Request):
        client = request.app.state.upstream
        async with request.app.state.accounts_lock:
            with request.app.state.store.begin() as db:
                row = find(db, Account, account_id)
                values = body.model_dump(exclude_unset=True)
                if any(value is None for value in values.values()):
                    raise HTTPException(422, "Invalid account input")
                getter = getattr(client, "account_config", None)
                config, managed = getter(account_id) if getter else (None, False)
                if {"proxy_url", "password"} & values.keys():
                    if not managed:
                        raise HTTPException(409, "Account is not managed")
                    proxy = values.get("proxy_url", config.proxy_url)
                    try:
                        proxy_parts(proxy)
                    except ValueError:
                        raise HTTPException(422, "Invalid account input") from None
                    updated = replace(config, proxy_url=proxy, password=values.get("password", config.password))
                    if proxy != config.proxy_url:
                        updated = replace(updated, timezone=await timezone_for(updated))
                    try:
                        await client.replace_account(updated)
                    except (OSError, UpstreamError):
                        raise HTTPException(503, "Browser pool unavailable") from None
                if "status" in values:
                    status_values = {"status": values["status"]}
                    validate_account(db, row, status_values)
                    for key, value in status_values.items():
                        setattr(row, key, value)
                db.flush()
                return account_out(row, client)

    @app.delete("/accounts/{account_id}", status_code=204, dependencies=auth, operation_id="delete_accounts")
    async def delete_account(account_id: s.Ref, request: Request):
        client = request.app.state.upstream
        async with request.app.state.accounts_lock:
            with request.app.state.store.begin() as db:
                row = find(db, Account, account_id)
                getter = getattr(client, "account_config", None)
                _, managed = getter(account_id) if getter else (None, False)
                if not managed or db.scalar(select(Job.id).where(Job.account_id == account_id).limit(1)) is not None:
                    raise HTTPException(409, "Account cannot be deleted")
                try:
                    await client.remove_account(account_id)
                except (OSError, UpstreamError):
                    raise HTTPException(503, "Browser pool unavailable") from None
                db.delete(row)
                db.flush()
        return Response(status_code=204)

    @app.get("/accounts/{account_id}/session", response_model=s.AccountSession, dependencies=auth)
    async def account_session(account_id: s.Ref, db: DB, request: Request):
        find(db, Account, account_id)
        gate = request.app.state.gate
        return {"status": await gate.account_status(account_id) if gate else "unknown"}

    @app.get("/coins/{coin_id}/mapping", response_model=s.MappingInput, dependencies=auth)
    def get_mapping(coin_id: s.CoinRef, db: DB):
        return find(db, CoinMapping, coin_id)

    @app.put("/coins/{coin_id}/mapping", response_model=s.MappingInput, dependencies=auth)
    def put_mapping(coin_id: s.CoinRef, body: s.MappingInput, db: DB):
        coin = find(db, Coin, coin_id)
        db.merge(CoinMapping(coin_id=coin_id, pricing_id=body.pricing_id))
        db.flush()
        sync_coin(db, coin)
        return body

    @app.delete("/coins/{coin_id}/mapping", status_code=204, dependencies=auth)
    def delete_mapping(coin_id: s.CoinRef, db: DB):
        coin = find(db, Coin, coin_id)
        db.delete(find(db, CoinMapping, coin_id))
        db.flush()
        sync_coin(db, coin)
        return Response(status_code=204)

    @app.get("/transfers", response_model=list[s.TransferOut], dependencies=auth)
    def list_transfers(db: DB, coin_id: s.CoinRef | None = None, start: datetime | None = None, end: datetime | None = None,
                       min_usd: s.NonNegative | None = None, max_usd: s.NonNegative | None = None,
                       direction: s.TransferDirection | None = None, entity: str | None = None,
                       address: str | None = None, chain: str | None = None, txhash: str | None = None,
                       limit: Page = 100, offset: Offset = 0):
        query = add_bounds(select(Transfer), Transfer.time, utc_naive(start) if start else None,
                           utc_naive(end) if end else None)
        query = add_bounds(query, Transfer.usd, min_usd, max_usd)
        if coin_id:
            query = query.where(select(TransferCoin.transfer_id).where(
                TransferCoin.transfer_id == Transfer.id, TransferCoin.coin_id == coin_id).exists())
        if entity:
            query = query.where(or_(Transfer.from_entity == entity, Transfer.to_entity == entity))
        if address:
            query = query.where(or_(Transfer.from_address == address, Transfer.to_address == address))
        for field, value in ((Transfer.direction, direction), (Transfer.chain, chain), (Transfer.txhash, txhash)):
            if value is not None:
                query = query.where(field == value)
        rows = db.scalars(query.order_by(Transfer.time.desc(), Transfer.id.desc()).offset(offset).limit(limit)).all()
        ids = {row.id: [] for row in rows}
        for transfer_id, symbol in db.execute(select(TransferCoin.transfer_id, TransferCoin.coin_id).where(
            TransferCoin.transfer_id.in_(ids)).order_by(TransferCoin.coin_id)):
            ids[transfer_id].append(symbol)
        return [{key: (ids[row.id] if key == "coin_ids" else getattr(row, key)) for key in s.TransferOut.model_fields}
                for row in rows]

    @app.get("/events", response_model=list[s.EventOut], dependencies=auth)
    def list_events(db: DB, level: s.Level | None = None, kind: str | None = None, coin_id: s.CoinRef | None = None,
                    rule_id: int | None = None, read: bool | None = None, start: datetime | None = None,
                    end: datetime | None = None, limit: Page = 100, offset: Offset = 0):
        query = add_bounds(select(Event), Event.created_at, utc_naive(start) if start else None,
                           utc_naive(end) if end else None)
        for field, value in ((Event.level, level), (Event.kind, kind), (Event.coin_id, coin_id), (Event.rule_id, rule_id)):
            if value is not None:
                query = query.where(field == value)
        if read is not None:
            query = query.where(Event.read_at.is_not(None) if read else Event.read_at.is_(None))
        return db.scalars(query.order_by(Event.id.desc()).offset(offset).limit(limit)).all()

    @app.put("/events/{event_id}/read", response_model=s.EventOut, dependencies=auth)
    def mark_event_read(event_id: int, body: s.ReadInput, db: DB):
        row = find(db, Event, event_id)
        row.read_at = (row.read_at or now()) if body.read else None
        return row

    @app.get("/balances", response_model=s.BalanceCurve, dependencies=auth)
    def balance_curve(coin_id: s.CoinRef, entity_id: Annotated[list[str], Query(min_length=1, max_length=100)], db: DB,
                      start: date | None = None, end: date | None = None):
        find(db, Coin, coin_id)
        query = select(DailyBalance).where(DailyBalance.coin_id == coin_id, DailyBalance.entity_id.in_(entity_id))
        series = {entity: [] for entity in entity_id}
        for row in db.scalars(add_bounds(query, DailyBalance.day, start, end).order_by(DailyBalance.day)):
            series[row.entity_id].append(row)
        days = add_bounds(select(DailyCollection).where(DailyCollection.coin_id == coin_id), DailyCollection.day, start, end)
        return {"coin_id": coin_id, "days": db.scalars(days.order_by(DailyCollection.day)).all(), "series": series}

    @app.get("/coins/{coin_id}/entities", response_model=list[s.EntityBalanceOut], dependencies=auth)
    def coin_entities(coin_id: s.CoinRef, db: DB, days: Annotated[int, Query(ge=1, le=36500)] = 30):
        find(db, Coin, coin_id)
        latest = select(DailyBalance.entity_id, func.max(DailyBalance.day).label("day")).where(
            DailyBalance.coin_id == coin_id,
            DailyBalance.day >= now().date() - timedelta(days=days - 1),
        ).group_by(DailyBalance.entity_id).subquery()
        return db.scalars(select(DailyBalance).join(latest, (DailyBalance.entity_id == latest.c.entity_id) &
            (DailyBalance.day == latest.c.day)).where(DailyBalance.coin_id == coin_id)
            .order_by(DailyBalance.entity_id)).all()

    @app.post("/snapshots", status_code=201, response_model=s.SnapshotOut, dependencies=auth)
    async def create_snapshot(body: s.SnapshotInput, request: Request):
        collectors = request.app.state.collectors
        if not collectors:
            raise HTTPException(503, "Upstream not configured")
        try:
            return await collectors.snapshot(body.account_id, body.interval, body.balance_min, body.pricing_ids)
        except AccountUnavailable as exc:
            raise HTTPException(409, "Account unavailable") from exc
        except UpstreamError as exc:
            raise HTTPException(502, type(exc).__name__) from exc

    @app.get("/snapshots", response_model=list[s.SnapshotOut], dependencies=auth)
    def list_snapshots(db: DB, limit: Page = 100, offset: Offset = 0):
        return db.scalars(select(Snapshot).order_by(Snapshot.id.desc()).offset(offset).limit(limit)).all()

    def snapshot_rows(db, snapshot_id):
        return db.scalars(select(SnapshotRow).where(SnapshotRow.snapshot_id == snapshot_id)
                          .order_by(SnapshotRow.entity_id)).all()

    @app.get("/snapshots/compare", response_model=s.SnapshotDiff, dependencies=auth)
    def compare_snapshots(before: int, after: int, db: DB):
        for snapshot_id in (before, after):
            find(db, Snapshot, snapshot_id)
        old, new = ([s.SnapshotRowOut.model_validate(row).model_dump() for row in snapshot_rows(db, snapshot_id)]
                    for snapshot_id in (before, after))
        return snapshot_diff(old, new)

    @app.get("/snapshots/{snapshot_id}", response_model=s.SnapshotDetail, dependencies=auth)
    def get_snapshot(snapshot_id: int, db: DB):
        snapshot = s.SnapshotOut.model_validate(find(db, Snapshot, snapshot_id)).model_dump()
        return {**snapshot, "rows": snapshot_rows(db, snapshot_id)}

    @app.websocket("/ws/events")
    async def event_socket(websocket: WebSocket):
        header = websocket.headers.get("Authorization", "").split(maxsplit=1)
        if len(header) != 2 or header[0].lower() != "bearer" or not token_matches(header[1], app.state.settings):
            await websocket.close(code=1008)
            return
        store = app.state.store
        with store() as db:
            cursor = db.scalar(select(func.max(Event.id))) or 0
        await websocket.accept()
        while True:
            with store() as db:
                batch = db.scalars(select(Event).where(Event.id > cursor, Event.push).order_by(Event.id).limit(100)).all()
            for row in batch:
                await websocket.send_json(s.EventOut.model_validate(row).model_dump(mode="json"))
                with store.begin() as db:
                    db.add(PushLog(event_id=row.id))
                cursor = row.id
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=1)
            except TimeoutError:
                continue
            if message["type"] == "websocket.disconnect":
                break

    return app
