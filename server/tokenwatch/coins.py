from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select

from .collectors import BALANCE_PATH, balance_params, parse_balances
from .db import Coin, CoinMapping, Exclusion


SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
ALPHA_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"


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
        spot = await client.get(SPOT_URL)
        spot.raise_for_status()
        alpha = await client.get(ALPHA_URL)
        alpha.raise_for_status()
    candidates = {}
    for pair in spot.json()["symbols"]:
        if pair["status"] == "TRADING" and pair.get("isSpotTradingAllowed", True):
            for symbol in (pair["baseAsset"], pair["quoteAsset"]):
                candidates.setdefault(symbol.upper(), [{"feed": "spot"}])
    for token in alpha.json()["data"]:
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
            ambiguous = sum(source["feed"] == "alpha" for source in candidate.sources) > 1
            pricing_id = (override.pricing_id if override else None if ambiguous else
                          await self.resolver.resolve(candidate))
            with self.store.begin() as db:
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
                rows = await self.gate.get(job.account_id, BALANCE_PATH, balance_params("7d", None, [pricing_id]),
                                           parse_balances)
                with self.store.begin() as db:
                    coin = db.get(Coin, candidate.symbol)
                    if coin and coin.pricing_id == pricing_id and not db.get(Exclusion, candidate.symbol):
                        coin.verified = bool(rows)
                        coin.exclusion_reason = None if rows else "no_balance_data"
