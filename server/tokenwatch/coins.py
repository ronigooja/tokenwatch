import asyncio
import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from .db import CatalogCache, CatalogExclusion, CatalogSettings, ChainObservation, Coin, CoinMapping, Exclusion, FuturesMapping, Job
from .upstream import BadResponse, UpstreamError, TOKEN_ADDRESSES_PREFIX, UNSUPPORTED_TOKEN


SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
ALPHA_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
NETWORK_URL = "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll"
ASSET_URL = "https://www.binance.com/bapi/asset/v2/public/asset/asset/get-all-asset"
FUTURES_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SEARCH_PATH = "/intelligence/search"
EVM = re.compile(r"0x[0-9a-f]{40}", re.IGNORECASE)
PRICING_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
logger = logging.getLogger(__name__)
FEEDS = (("spot exchangeInfo", SPOT_URL), ("alpha token list", ALPHA_URL),
         ("network coins", NETWORK_URL), ("asset list", ASSET_URL), ("futures exchangeInfo", FUTURES_URL))


async def public_json(client, url, sleep=asyncio.sleep):
    for attempt in range(3):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == 2 or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 451):
                raise
            await sleep(30 * 2 ** attempt)


def field(row, name, kind, *, empty=False):
    value = row.get(name) if isinstance(row, dict) else None
    if type(value) is not kind or (kind in (str, list) and not value and not empty):
        raise ValueError(f"invalid {name}")
    return value


def envelope(payload):
    if field(payload, "code", str) != "000000":
        raise ValueError("unsuccessful Binance response")
    return field(payload, "data", list)


def network(row):
    def optional(name, kind):
        value = row.get(name) if isinstance(row, dict) else None
        return value if type(value) is kind and (kind is not str or value) else None

    raw_address = row.get("contractAddress") if isinstance(row, dict) else None
    return {"chain": optional("network", str), "address": optional("contractAddress", str),
            "address_known": raw_address is None or type(raw_address) is str,
            "deposit_enabled": optional("depositEnable", bool), "withdraw_enabled": optional("withdrawEnable", bool),
            "feed": "spot"}


def indexed(rows, key, parse):
    result = {}
    for row in rows:
        symbol = field(row, key, str).upper()
        if symbol in result:
            raise ValueError(f"repeated {key}")
        result[symbol] = parse(row)
    return result


def parse_assets(row):
    tags = row.get("tags")
    if tags is None:
        tags = []
    elif not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        tags = None
    else:
        tags = [tag for tag in tags if tag]
    return {"fiat": field(row, "isLegalMoney", bool), "ledger_only": field(row, "isLedgerOnly", bool), "tags": tags}


def parse_feeds(spot, alpha, networks, assets, futures):
    symbols, tokens, bases = set(), [], set()
    for pair in field(spot, "symbols", list):
        status = field(pair, "status", str)
        symbol = field(pair, "baseAsset", str).upper()
        if field(pair, "isSpotTradingAllowed", bool) and status == "TRADING":
            symbols.add(symbol)
    for row in envelope(alpha):
        chain = row.get("chainId") if isinstance(row, dict) else None
        if not (type(chain) is str and chain or type(chain) is int and chain > 0):
            raise ValueError("invalid Alpha chainId")
        tokens.append({"symbol": field(row, "symbol", str).upper(), "chain": str(chain),
                       "address": field(row, "contractAddress", str)})
    for row in field(futures, "symbols", list):
        base = field(row, "baseAsset", str).upper()
        if [field(row, name, str, empty=name == "contractType")
                for name in ("contractType", "status", "underlyingType", "quoteAsset", "marginAsset")] == [
                "PERPETUAL", "TRADING", "COIN", "USDT", "USDT"]:
            bases.add(base)
    snapshot = {"spot": sorted(symbols), "alpha": tokens, "futures": sorted(bases),
            "networks": indexed(envelope(networks), "coin", lambda row: {
                "fiat": field(row, "isLegalMoney", bool),
                "deployments": [network(item) for item in field(row, "networkList", list, empty=True)]}),
            "assets": indexed(envelope(assets), "assetCode", parse_assets)}
    identities = [row.id for row in candidates(snapshot)]
    if len(identities) != len(set(identities)):
        raise ValueError("Binance assets share a canonical identity")
    return snapshot


def normalize(address):
    return address.lower() if EVM.fullmatch(address) else address


def key_part(value):
    return value if re.fullmatch(r"[a-zA-Z0-9_.-]+", value) else "~" + value.encode().hex()


@dataclass
class Candidate:
    id: str
    symbol: str
    feed: str
    deployments: list[dict]


def deployment_keys(deployments):
    return {(row["chain"], normalize(row["address"])) for row in deployments if row["chain"] and row["address"]}


def candidates(snapshot):
    for symbol in snapshot["spot"]:
        deployments = snapshot["networks"].get(symbol, {}).get("deployments", [])
        usable = [row for row in deployments if row["chain"] and row.get("address_known", True) and (
            row["address"] or row["chain"] == symbol and (row["deposit_enabled"] or row["withdraw_enabled"]))]
        if usable:
            # A same-name native network takes precedence over bridged deployments.
            anchor = min(usable, key=lambda row: (bool(row["address"] or row["chain"] != symbol), not row["address"],
                                                row["chain"], normalize(row["address"] or "")))
            chain, address = key_part(anchor["chain"]), anchor["address"]
            key = f"spot:{chain}:{key_part(normalize(address))}" if address else f"native:{chain}"
        else:
            key = f"unknown:spot:{key_part(symbol)}"
        yield Candidate(key, symbol, "spot", deployments)
    grouped = defaultdict(list)
    for row in snapshot["alpha"]:
        key = f"alpha:{key_part(row['chain'])}:{key_part(normalize(row['address']))}"
        grouped[key].append(row)
    for key, rows in sorted(grouped.items()):
        # One asset can have multiple display aliases in the feed, but never multiple identities at one address.
        row = min(rows, key=lambda item: (item["symbol"], item["address"]))
        yield Candidate(key, row["symbol"], "alpha", [{"feed": "alpha", "chain": row["chain"],
                                                       "address": row["address"], "deposit_enabled": None,
                                                       "withdraw_enabled": None}])


def classify(candidate, snapshot, futures_symbols, policies, mapped=False):
    metadata = snapshot["assets"].get(candidate.symbol)
    networks = snapshot["networks"].get(candidate.symbol)
    # A spot asset's exchange labels are not proof of the identity of another contract with the same symbol.
    if candidate.feed == "spot" or (networks and any(
        row["address"] and normalize(row["address"]) == normalize(candidate.deployments[0]["address"])
        for row in networks["deployments"]
    )):
        reasons = {reason for kind, value, reason in policies if (
            kind == "flag" and value == "isLegalMoney" and (
                (metadata and metadata["fiat"]) or (networks and networks["fiat"]))
             or kind == "tag" and metadata and metadata["tags"] is not None and value in metadata["tags"])}
        if reasons:
            return "excluded", "; ".join(sorted(reasons))
    if candidate.feed == "spot" and (not metadata or metadata["tags"] is None or not mapped and not any(
        row["chain"] and row.get("address_known", True) and (
            row["address"] or row["chain"] == candidate.symbol and (
                row["deposit_enabled"] or row["withdraw_enabled"])) for row in candidate.deployments
    )):
        return "unknown_onchain", "missing asset metadata or usable Binance network"
    if candidate.symbol not in futures_symbols:
        return "no_futures", "no trading USDT-margined coin perpetual"
    return None, None


def search_matches(payload, address):
    if not isinstance(payload, dict):
        raise BadResponse("invalid token search payload")
    tokens = payload.get("tokens")
    if tokens is None:
        tokens = []
    if not isinstance(tokens, list):
        raise BadResponse("invalid token search payload")
    matches = defaultdict(set)
    for token in tokens:
        identifier = token.get("identifier") if isinstance(token, dict) else None
        if not isinstance(identifier, dict):
            continue
        pricing_id = identifier.get("pricingID")
        if not isinstance(pricing_id, str) or not PRICING_ID.fullmatch(pricing_id) or ".." in pricing_id:
            continue
        deployments = token.get("deployments")
        deployments = deployments if isinstance(deployments, list) else []
        for deployment in [identifier, *deployments]:
            if not isinstance(deployment, dict):
                continue
            chain, contract = deployment.get("chain"), deployment.get("address")
            if isinstance(chain, str) and chain and isinstance(contract, str) and contract and normalize(contract) == normalize(address):
                matches[pricing_id].add(chain)
    return matches


def search_candidates(payload):
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    result = []
    for token in tokens if isinstance(tokens, list) else []:
        if not isinstance(token, dict):
            continue
        identifier = token.get("identifier")
        pricing_id = identifier.get("pricingID") if isinstance(identifier, dict) else None
        if not isinstance(pricing_id, str) or not PRICING_ID.fullmatch(pricing_id) or ".." in pricing_id:
            continue
        item = {"pricing_id": pricing_id}
        for key in ("name", "symbol"):
            if isinstance(token.get(key), str):
                item[key] = token[key]
        for key in ("rank", "marketCap", "market_cap", "price"):
            value = token.get(key)
            if type(value) is int or type(value) is float and math.isfinite(value):
                item[key] = value
        deployments = token.get("deployments")
        if isinstance(deployments, list):
            item["chains"] = list(dict.fromkeys(d["chain"] for d in deployments
                                if isinstance(d, dict) and isinstance(d.get("chain"), str) and d["chain"]))
        result.append(item)
        if len(result) == 20:
            break
    return result


def support_status(payload):
    if payload is UNSUPPORTED_TOKEN:
        return "unsupported"
    if not isinstance(payload, dict) or not payload or not all(
        isinstance(chain, str) and chain and isinstance(value, str) for chain, value in payload.items()
    ) or "message" in payload:
        raise BadResponse("invalid token addresses payload")
    return "supported"


def sync_coin(db, coin):
    """Project catalog and matching state into the API/job-facing fields."""
    manual = db.get(CoinMapping, coin.id)
    exclusion = db.get(Exclusion, coin.id)
    coin.pricing_id = manual.pricing_id if manual else coin.automatic_pricing_id
    coin.matched_chains = coin.automatic_chains if coin.pricing_id == coin.automatic_pricing_id else []
    if exclusion:
        coin.status, coin.reason = "excluded", exclusion.reason
    elif coin.catalog_status:
        coin.status, coin.reason = coin.catalog_status, coin.catalog_reason
    elif manual:
        coin.status = coin.manual_status if coin.manual_pricing_id == manual.pricing_id else "pending_support"
        coin.status = coin.status or "pending_support"
        coin.reason = coin.match_reason if coin.status == "match_failed" else ("Arkham does not support the manual pricing ID" if coin.status == "unsupported" else None)
    elif coin.match_status:
        coin.status, coin.reason = coin.match_status, coin.match_reason
    elif not coin.id.startswith("native:") and any(row["chain"] and row["address"] for row in coin.deployments):
        coin.status, coin.reason = "pending_match", None
    else:
        coin.status, coin.reason = "needs_manual_mapping", "no searchable contract address"


class CoinCatalog:
    def __init__(self, store, gate, transport=None, clock=time.time, settings=None):
        self.store, self.gate, self.transport, self.clock = store, gate, transport, clock
        self.settings = settings
        self.refresh_lock, self.match_lock = asyncio.Lock(), asyncio.Lock()

    async def refresh_snapshot(self):
        async with self.refresh_lock:
            stamp = self.clock()
            feed = "client"
            try:
                proxy = getattr(self.settings, "binance_proxy_url", None)
                async with httpx.AsyncClient(timeout=20, transport=self.transport,
                                             proxy=proxy.get_secret_value() if proxy else None) as client:
                    feeds = []
                    for feed, url in FEEDS:
                        feeds.append(await public_json(client, url))
                feed = "catalog"
                snapshot = parse_feeds(*feeds)
                error = None
            except httpx.HTTPError as exc:
                status = f" HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else ""
                snapshot, error = None, f"Binance {feed}: {type(exc).__name__}{status}"[:200]
            except ValueError as exc:
                snapshot, error = None, f"Invalid Binance {feed}: {type(exc).__name__}"[:200]
            with self.store.begin() as db:
                cache = db.get(CatalogCache, "binance")
                if cache is None:
                    cache = CatalogCache(key="binance")
                    db.add(cache)
                if snapshot is not None and cache.payload:
                    previous = cache.payload
                    for symbol in set(snapshot["spot"]) & set(previous["spot"]):
                        if (symbol in previous["assets"] and symbol not in snapshot["assets"] or
                                previous["networks"].get(symbol, {}).get("deployments") and
                                not snapshot["networks"].get(symbol, {}).get("deployments")):
                            snapshot, error = None, "Binance catalog lost metadata for a trading spot asset"
                            break
                cache.attempted_at, cache.error = stamp, error
                if snapshot is not None:
                    cache.payload, cache.refreshed_at = snapshot, stamp
                self.reclassify(db, cache.payload)
            if error:
                logger.warning("%s", error)
            return error

    @staticmethod
    def reclassify(db, snapshot):
        if snapshot is None:
            return
        rows = list(candidates(snapshot))
        symbols = {candidate.symbol for candidate in rows}
        mappings = {row.base: row.symbol for row in db.scalars(select(FuturesMapping))}
        futures = {base if base in symbols else mappings.get(base) for base in snapshot["futures"]}
        policies = [(row.kind, row.value, row.reason) for row in db.scalars(select(CatalogExclusion))]
        mapped_ids = set(db.scalars(select(CoinMapping.coin_id)))
        existing = {row.id: row for row in db.scalars(select(Coin))}
        for candidate in rows:
            coin = existing.pop(candidate.id, None)
            if coin is None:
                coin = Coin(id=candidate.id, automatic_chains=[])
                db.add(coin)
            elif coin.match_status and deployment_keys(coin.deployments).isdisjoint(deployment_keys(candidate.deployments)):
                coin.match_status = coin.match_reason = coin.automatic_pricing_id = None
                coin.automatic_chains = []
                coin.searched_deployments = []
            coin.symbol, coin.sources, coin.deployments = candidate.symbol, [candidate.feed], candidate.deployments
            coin.catalog_status, coin.catalog_reason = classify(candidate, snapshot, futures, policies, candidate.id in mapped_ids)
            sync_coin(db, coin)
        for coin in existing.values():
            coin.catalog_status, coin.catalog_reason = "unknown_onchain", "not in the validated Binance catalog"
            sync_coin(db, coin)

    async def refresh(self, job):
        if not getattr(job, "wait_reason", None):
            with self.store() as db:
                setting = db.get(CatalogSettings, "matching")
                if setting is None or setting.refresh_retry_seconds is None or setting.refresh_retry_seconds < 1:
                    raise MissingCatalogRefreshRetry()
                retry = setting.refresh_retry_seconds
            error = await self.refresh_snapshot()
            if error:
                raise CatalogRefreshFailure(error, self.clock() + retry)
        fields = ("account_id", "kind", "options", "interval_seconds", "coin_id")
        while not self.gate.closing:
            with self.store() as db:
                current = db.execute(select(Job.enabled, *(getattr(Job, field) for field in fields))
                                     .where(Job.id == job.id)).first()
            if not current or not current.enabled or any(
                getattr(current, field) != getattr(job, field) for field in fields
            ):
                return
            if not await self.match_next(job):
                return

    async def match_next(self, job):
        async with self.match_lock:
            with self.store() as db:
                coin = db.scalar(select(Coin).where(Coin.status.in_(("pending_match", "pending_support")))
                                 .order_by(Coin.last_match_attempt, Coin.id).limit(1))
                if coin is None:
                    return False
                setting = db.get(CatalogSettings, "matching")
                if setting is None or setting.failure_limit < 1:
                    raise MissingCatalogFailureLimit()
                limit = setting.failure_limit
                manual = db.get(CoinMapping, coin.id)
            async def request(path, params, decode):
                try:
                    result = await self.gate.get(job.account_id, path, params, decode)
                except UpstreamError as exc:
                    with self.store.begin() as db:
                        current = db.get(Coin, coin.id)
                        if current and current.status == coin.status and current.pricing_id == coin.pricing_id:
                            current.last_match_attempt = self.clock()
                            current.match_failures += 1
                            if current.match_failures >= limit:
                                current.match_reason = f"{type(exc).__name__}: {current.match_failures} consecutive failures"
                                if manual:
                                    current.manual_pricing_id, current.manual_status = manual.pricing_id, "match_failed"
                                else:
                                    current.match_status = "match_failed"
                                sync_coin(db, current)
                    raise
                with self.store.begin() as db:
                    current = db.get(Coin, coin.id)
                    if current and current.status == coin.status and current.pricing_id == coin.pricing_id:
                        current.last_match_attempt, current.match_failures = self.clock(), 0
                return result
            if coin.status == "pending_match":
                searched = {tuple(key) for key in coin.searched_deployments}
                deployment = min((row for row in coin.deployments if row["chain"] and row["address"] and
                                  (row["chain"], normalize(row["address"])) not in searched),
                                 key=lambda row: (row["chain"], normalize(row["address"])), default=None)
                if deployment is None:
                    with self.store.begin() as db:
                        current = db.get(Coin, coin.id)
                        if current and current.status == "pending_match" and not (
                            deployment_keys(current.deployments) - {tuple(key) for key in current.searched_deployments}
                        ):
                            current.match_status, current.match_reason = "no_match", "no Arkham token matches the contract"
                            sync_coin(db, current)
                    return True
                address = deployment["address"]
                matches = await request(SEARCH_PATH, {"query": address},
                                              lambda payload: search_matches(payload, address))
                with self.store.begin() as db:
                    current = db.get(Coin, coin.id)
                    if current and current.match_status is None and (
                        deployment["chain"], normalize(address)
                    ) in deployment_keys(current.deployments):
                        if len(matches) == 1:
                            pricing_id, chains = next(iter(matches.items()))
                            current.automatic_pricing_id, current.automatic_chains = pricing_id, sorted(chains)
                            current.match_status, current.match_reason = "pending_support", None
                            if deployment["feed"] == "alpha":
                                for chain in chains:
                                    db.merge(ChainObservation(alpha_chain_id=deployment["chain"], arkham_chain=chain))
                        elif matches:
                            current.match_status, current.match_reason = "ambiguous", "multiple Arkham pricing IDs match the contract"
                        else:
                            key = (deployment["chain"], normalize(address))
                            searched = {tuple(item) for item in current.searched_deployments} | {key}
                            current.searched_deployments = [list(item) for item in sorted(searched)]
                            if not deployment_keys(current.deployments) - searched:
                                current.match_status, current.match_reason = "no_match", "no Arkham token matches the contract"
                        sync_coin(db, current)
            else:
                status = await request(TOKEN_ADDRESSES_PREFIX + coin.pricing_id, None, support_status)
                with self.store.begin() as db:
                    current = db.get(Coin, coin.id)
                    if current and manual:
                        override = db.get(CoinMapping, coin.id)
                        if override and override.pricing_id == manual.pricing_id:
                            current.manual_pricing_id, current.manual_status = manual.pricing_id, status
                    elif current and current.automatic_pricing_id == coin.pricing_id:
                        current.match_status = status
                        current.match_reason = "Arkham does not support this pricing ID" if status == "unsupported" else None
                    if current:
                        sync_coin(db, current)
            return True


class MissingCatalogFailureLimit(Exception):
    """Matching requires an explicitly configured failure limit."""


class MissingCatalogRefreshRetry(Exception):
    """Catalog refresh requires an explicitly configured retry delay."""


class CatalogRefreshFailure(Exception):
    def __init__(self, error, retry_at):
        self.error, self.retry_at = error, retry_at
