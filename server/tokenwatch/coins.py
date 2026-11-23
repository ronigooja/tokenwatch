import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from .db import CatalogCache, CatalogExclusion, ChainObservation, Coin, CoinMapping, Exclusion, FuturesMapping
from .upstream import BadResponse, TOKEN_ADDRESSES_PREFIX, UNSUPPORTED_TOKEN


SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
ALPHA_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
NETWORK_URL = "https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll"
ASSET_URL = "https://www.binance.com/bapi/asset/v2/public/asset/asset/get-all-asset"
FUTURES_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SEARCH_PATH = "/intelligence/search"
EVM = re.compile(r"0x[0-9a-f]{40}", re.IGNORECASE)
PRICING_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


async def public_json(client, url, sleep=asyncio.sleep):
    for attempt in range(3):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
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


def classify(candidate, snapshot, futures_symbols, policies):
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
    if candidate.feed == "spot" and (not metadata or metadata["tags"] is None or not any(
        row["chain"] and row.get("address_known", True) and (
            row["address"] or row["chain"] == candidate.symbol and (
                row["deposit_enabled"] or row["withdraw_enabled"])) for row in candidate.deployments
    )):
        return "unknown_onchain", "missing asset metadata or usable Binance network"
    if candidate.symbol not in futures_symbols:
        return "no_futures", "no trading USDT-margined coin perpetual"
    return None, None


def search_matches(payload, address):
    if not isinstance(payload, dict) or not isinstance(payload.get("tokens"), list):
        raise BadResponse("invalid token search payload")
    matches = defaultdict(set)
    for token in payload["tokens"]:
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
        coin.reason = "Arkham does not support the manual pricing ID" if coin.status == "unsupported" else None
    elif coin.match_status:
        coin.status, coin.reason = coin.match_status, coin.match_reason
    elif not coin.id.startswith("native:") and any(row["chain"] and row["address"] for row in coin.deployments):
        coin.status, coin.reason = "pending_match", None
    else:
        coin.status, coin.reason = "needs_manual_mapping", "no searchable contract address"


class CoinCatalog:
    def __init__(self, store, gate, transport=None, clock=time.time):
        self.store, self.gate, self.transport, self.clock = store, gate, transport, clock
        self.refresh_lock, self.match_lock = asyncio.Lock(), asyncio.Lock()

    async def refresh_snapshot(self):
        async with self.refresh_lock:
            stamp = self.clock()
            try:
                async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                    feeds = [await public_json(client, url)
                             for url in (SPOT_URL, ALPHA_URL, NETWORK_URL, ASSET_URL, FUTURES_URL)]
                snapshot = parse_feeds(*feeds)
                error = None
            except httpx.HTTPError:
                snapshot, error = None, "Binance catalog request failed"
            except ValueError as exc:
                snapshot, error = None, f"Invalid Binance catalog: {exc}"
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

    @staticmethod
    def reclassify(db, snapshot):
        if snapshot is None:
            return
        rows = list(candidates(snapshot))
        symbols = {candidate.symbol for candidate in rows}
        mappings = {row.base: row.symbol for row in db.scalars(select(FuturesMapping))}
        futures = {base if base in symbols else mappings.get(base) for base in snapshot["futures"]}
        policies = [(row.kind, row.value, row.reason) for row in db.scalars(select(CatalogExclusion))]
        existing = {row.id: row for row in db.scalars(select(Coin))}
        for candidate in rows:
            coin = existing.pop(candidate.id, None)
            if coin is None:
                coin = Coin(id=candidate.id, automatic_chains=[])
                db.add(coin)
            elif coin.match_status and deployment_keys(coin.deployments).isdisjoint(deployment_keys(candidate.deployments)):
                coin.match_status = coin.match_reason = coin.automatic_pricing_id = None
                coin.automatic_chains = []
            coin.symbol, coin.sources, coin.deployments = candidate.symbol, [candidate.feed], candidate.deployments
            coin.catalog_status, coin.catalog_reason = classify(candidate, snapshot, futures, policies)
            sync_coin(db, coin)
        for coin in existing.values():
            coin.catalog_status, coin.catalog_reason = "unknown_onchain", "not in the validated Binance catalog"
            sync_coin(db, coin)

    async def refresh(self, job):
        if not getattr(job, "wait_reason", None):
            await self.refresh_snapshot()
        while await self.match_next(job):
            pass

    async def match_next(self, job):
        async with self.match_lock:
            with self.store() as db:
                coin = db.scalar(select(Coin).where(Coin.status.in_(("pending_match", "pending_support")))
                                 .order_by(Coin.id).limit(1))
                if coin is None:
                    return False
                manual = db.get(CoinMapping, coin.id)
            if coin.status == "pending_match":
                deployment = min((row for row in coin.deployments if row["chain"] and row["address"]),
                                 key=lambda row: (row["chain"], normalize(row["address"])))
                address = deployment["address"]
                matches = await self.gate.get(job.account_id, SEARCH_PATH, {"query": address},
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
                            current.match_status, current.match_reason = "no_match", "no Arkham token matches the contract"
                        sync_coin(db, current)
            else:
                status = await self.gate.get(job.account_id, TOKEN_ADDRESSES_PREFIX + coin.pricing_id, None, support_status)
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
