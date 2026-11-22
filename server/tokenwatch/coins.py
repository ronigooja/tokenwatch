import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select

from .collectors import BALANCE_PATH, balance_params, parse_balances
from .db import CatalogCache, CatalogMapping, Coin, CoinMapping, Exclusion


SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
ALPHA_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
GECKO_URL = "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
WEEK = 7 * 24 * 3600
# Only explicit chain identities are eligible for contract matching.
PLATFORMS = {"1": "ethereum", "56": "binance-smart-chain", "137": "polygon-pos",
             "42161": "arbitrum-one", "10": "optimistic-ethereum", "8453": "base",
             "43114": "avalanche", "solana": "solana", "CT_501": "solana"}


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


class CoinGeckoResolver:
    def __init__(self, store, transport=None, clock=time.time):
        self.store, self.transport, self.clock = store, transport, clock
        self.lock = asyncio.Lock()

    async def coins(self):
        async with self.lock:
            stamp = self.clock()
            with self.store.begin() as db:
                cache = db.get(CatalogCache, "coingecko")
                if cache and stamp - cache.attempted_at < WEEK:
                    return cache.payload
                if cache is None:
                    cache = CatalogCache(key="coingecko", payload=[])
                    db.add(cache)
                cache.attempted_at = stamp
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                payload = await public_json(client, GECKO_URL)
            if not isinstance(payload, list) or not all(
                isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] and
                isinstance(row.get("symbol"), str) and isinstance(row.get("platforms"), dict) and
                all(isinstance(k, str) and isinstance(v, (str, type(None))) for k, v in row["platforms"].items())
                for row in payload
            ):
                raise ValueError("Invalid CoinGecko catalog")
            with self.store.begin() as db:
                cache = db.get(CatalogCache, "coingecko")
                cache.payload, cache.refreshed_at = payload, stamp
            return payload

    async def resolve(self, candidate):
        coins = await self.coins()
        contracts = set()
        for source in candidate.sources:
            platform = PLATFORMS.get(source.get("chain_id"))
            address = source.get("contract_address")
            if not platform or not address:
                continue
            normalize = str.lower if address.startswith("0x") else lambda value: value
            contracts.update(row["id"] for row in coins if row["platforms"].get(platform) and
                             normalize(row["platforms"][platform]) == normalize(address))
        matches = contracts
        if not matches and any(source["feed"] == "spot" for source in candidate.sources):
            matches = {row["id"] for row in coins if row["symbol"].upper() == candidate.symbol}
        ambiguous = len(matches) > 1
        pricing_id = next(iter(matches)) if len(matches) == 1 else None
        with self.store.begin() as db:
            db.merge(CatalogMapping(symbol=candidate.symbol, pricing_id=pricing_id, ambiguous=ambiguous))
        return pricing_id


@dataclass(frozen=True)
class Candidate:
    symbol: str
    sources: list[dict]


class Resolver(Protocol):
    async def resolve(self, candidate: Candidate) -> str | None: ...


class KnownPricingIDResolver:
    def __init__(self, ids: dict[str, str]):
        self.ids = ids

    async def resolve(self, candidate: Candidate) -> str | None:
        return self.ids.get(candidate.symbol)


async def binance_candidates(transport=None):
    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        spot = await public_json(client, SPOT_URL)
        alpha = await public_json(client, ALPHA_URL)
    if not isinstance(spot, dict) or not isinstance(spot.get("symbols"), list):
        raise ValueError("Invalid Binance spot catalog")
    if not isinstance(alpha, dict) or alpha.get("code") != "000000" or not isinstance(alpha.get("data"), list):
        raise ValueError("Invalid Binance Alpha catalog")
    candidates = {}
    for pair in spot["symbols"]:
        if not isinstance(pair, dict) or not all(isinstance(pair.get(k), str) and pair[k] for k in ("status", "baseAsset")):
            raise ValueError("Invalid Binance spot entry")
        if pair["status"] == "TRADING" and pair.get("isSpotTradingAllowed", True):
            candidates.setdefault(pair["baseAsset"].upper(), [{"feed": "spot"}])
    for token in alpha["data"]:
        if not isinstance(token, dict) or not all(isinstance(token.get(k), str) and token[k] for k in ("symbol", "contractAddress")) or not isinstance(token.get("chainId"), (str, int)):
            raise ValueError("Invalid Binance Alpha entry")
        source = {"feed": "alpha", "chain_id": str(token["chainId"]), "contract_address": token["contractAddress"]}
        sources = candidates.setdefault(token["symbol"].upper(), [])
        if source not in sources:
            sources.append(source)
    return [Candidate(symbol, sources) for symbol, sources in sorted(candidates.items())]


class CoinCatalog:
    def __init__(self, store, gate, settings, resolver, candidates=binance_candidates):
        self.store, self.gate, self.settings, self.resolver, self.candidates = store, gate, settings, resolver, candidates

    async def refresh(self, job):
        candidates = await self.candidates()
        with self.store.begin() as db:
            seen = {candidate.symbol for candidate in candidates}
            for coin in db.scalars(select(Coin)):
                if coin.sources and coin.id not in seen:
                    coin.verified = False
                    coin.exclusion_reason = "not_in_binance_feeds"
        for candidate in candidates:
            with self.store() as db:
                override = db.get(CoinMapping, candidate.symbol)
            pricing_id = override.pricing_id if override else await self.resolver.resolve(candidate)
            with self.store.begin() as db:
                mapping = db.get(CatalogMapping, candidate.symbol)
                ambiguous = bool(not override and mapping and mapping.ambiguous)
                coin = db.get(Coin, candidate.symbol)
                if coin is None:
                    coin = Coin(id=candidate.symbol, symbol=candidate.symbol)
                    db.add(coin)
                checked_empty = coin.pricing_id == pricing_id and coin.exclusion_reason == "no_balance_data"
                if coin.pricing_id != pricing_id:
                    coin.verified = False
                coin.pricing_id, coin.sources = pricing_id, candidate.sources
                if db.get(Exclusion, candidate.symbol):
                    coin.exclusion_reason = "globally_excluded"
                elif not pricing_id:
                    coin.exclusion_reason = "ambiguous_symbol" if ambiguous else "mapping_required"
                elif checked_empty:
                    coin.exclusion_reason = "no_balance_data"
                elif not coin.verified and not self.settings.allow_pricing_id_checks:
                    coin.exclusion_reason = "verification_disabled"
                elif not coin.verified:
                    coin.exclusion_reason = "verification_pending"
                else:
                    coin.exclusion_reason = None
                should_check = coin.exclusion_reason == "verification_pending"
            if should_check:
                params = balance_params("7d", None, [pricing_id])
                params["limit"] = "1"
                rows = await self.gate.get(job.account_id, BALANCE_PATH, params,
                                           parse_balances)
                with self.store.begin() as db:
                    coin = db.get(Coin, candidate.symbol)
                    if coin and coin.pricing_id == pricing_id and not db.get(Exclusion, candidate.symbol):
                        coin.verified = bool(rows)
                        coin.exclusion_reason = None if rows else "no_balance_data"
