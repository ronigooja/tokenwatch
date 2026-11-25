import math
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from .db import (Coin, DailyBalance, DailyCollection, Exclusion, Snapshot, SnapshotRow,
                 Subscription, Transfer, TransferCoin, TransferCursor, now)
from .rules import alert, evaluate
from .upstream import BadResponse


BALANCE_PATH = "/intelligence/entity_balance_changes"
BALANCE_LIMIT = 50
TRANSFER_LIMIT = 100


def balance_params(interval, balance_min, pricing_ids):
    params = {"interval": interval, "orderBy": "balance_usd_pct_change", "orderDir": "desc",
              "limit": str(BALANCE_LIMIT), "offset": "0"}
    if balance_min is not None:
        if (type(balance_min) not in (int, float) or balance_min < 0 or
                type(balance_min) is float and (not math.isfinite(balance_min) or not balance_min.is_integer())):
            raise ValueError("balance_min must be a non-negative integer")
        params["balanceMin"] = str(int(balance_min))
    if pricing_ids:
        params["pricingIds"] = ",".join(pricing_ids)
    return params


def number(value):
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite number")
    return result


def entity_fields(entity):
    return entity.get("id"), entity.get("name"), entity.get("type")


def parse_balances(payload, require_totals=False):
    try:
        if not isinstance(payload, list) or len(payload) > BALANCE_LIMIT:
            raise ValueError("invalid balance page")
        rows = {}
        for raw in payload:
            entity = raw.get("entity") or raw.get("arkhamEntity") or {
                "id": raw.get("entityId", raw.get("id")), "name": raw.get("entityName", raw.get("name")),
                "type": raw.get("entityType", raw.get("type")),
            }
            entity_id, entity_name, entity_type = entity_fields(entity)
            if not isinstance(entity_id, str) or not entity_id or entity_id in rows:
                raise ValueError("missing or repeated balance entity ID")
            row = {"entity_id": entity_id, "entity_name": entity_name, "entity_type": entity_type,
                   "balance_unit": number(raw.get("balanceUnit")), "balance_usd": number(raw.get("balanceUsd")),
                   "prev_balance_unit": number(raw.get("prevBalanceUnit")),
                   "prev_balance_usd": number(raw.get("prevBalanceUsd")), "token_balances": raw.get("tokenBalances")}
            if require_totals and (row["balance_unit"] is None or row["balance_usd"] is None):
                raise ValueError("missing balance totals")
            rows[entity_id] = row
        return list(rows.values())
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise BadResponse("invalid balance payload") from exc


def parse_transfers(payload):
    try:
        raw_transfers = payload["transfers"]
        if not isinstance(raw_transfers, list) or len(raw_transfers) > TRANSFER_LIMIT:
            raise ValueError("invalid transfer page")
        transfers = {}
        for raw in raw_transfers:
            transfer_id = raw["id"]
            if not isinstance(transfer_id, str) or not transfer_id:
                raise ValueError("invalid transfer ID")
            sender, recipient = raw.get("fromAddress") or {}, raw.get("toAddress") or {}
            from_entity = (sender.get("arkhamEntity") or {}).get("id")
            to_entity = (recipient.get("arkhamEntity") or {}).get("id")
            direction = ("internal" if from_entity and from_entity == to_entity else
                         "entity_to_entity" if from_entity and to_entity else "outbound" if from_entity else
                         "inbound" if to_entity else "unknown")
            stamp = datetime.fromisoformat(raw["blockTimestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
            transfers[transfer_id] = {"id": transfer_id, "txhash": raw.get("transactionHash"),
                                      "chain": raw.get("chain") or sender.get("chain") or recipient.get("chain"),
                                      "from_address": sender.get("address"), "to_address": recipient.get("address"),
                                      "from_entity": from_entity, "to_entity": to_entity, "direction": direction,
                                      "usd": number(raw.get("historicalUSD", raw.get("usdValue"))), "time": stamp}
        return transfers
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise BadResponse("invalid transfer payload") from exc


def eligible_coins():
    return select(Coin).where(Coin.status == "supported", Coin.pricing_id.is_not(None),
                              ~select(Exclusion.coin_id).where(Exclusion.coin_id == Coin.id).exists())


class Collectors:
    def __init__(self, store, gate, settings, clock=time.time, today=lambda: now().date(), catalog=None):
        self.store, self.gate, self.settings, self.clock, self.today = store, gate, settings, clock, today
        self.catalog = catalog

    async def transfers(self, job):
        with self.store() as db:
            coin = db.scalar(eligible_coins().join(Subscription, Subscription.coin_id == Coin.id).where(
                Coin.id == job.coin_id, Subscription.enabled))
        if not coin:
            return None
        rows = await self.gate.get(job.account_id, "/transfers", {
            "base": "all", "flow": "all", "sortKey": "time", "sortDir": "desc",
            "limit": str(TRANSFER_LIMIT), "offset": "0", "tokens": coin.pricing_id,
        }, parse_transfers)
        stamp = self.clock()
        minimum, maximum = self.settings.transfer_min_interval, self.settings.transfer_max_interval
        with self.store.begin() as db:
            if db.scalar(eligible_coins().join(Subscription, Subscription.coin_id == Coin.id).where(
                Coin.id == coin.id, Coin.pricing_id == coin.pricing_id, Subscription.enabled)) is None:
                return None
            cursor = db.get(TransferCursor, coin.id)
            baseline = cursor if cursor and cursor.pricing_id == coin.pricing_id else None
            linked = set(db.scalars(select(TransferCoin.transfer_id).where(TransferCoin.coin_id == coin.id,
                                                                          TransferCoin.transfer_id.in_(rows))))
            known = linked | (set(baseline.ids) if baseline else set())
            new_ids = set(rows) - known
            gap = bool(rows and baseline and baseline.ids and not known.intersection(rows))
            rate = baseline.rate if baseline else None
            if rate is None and len(rows) >= 2:
                times = [row["time"] for row in rows.values()]
                span = (max(times) - min(times)).total_seconds()
                if span > 0:
                    rate = len(rows) / span
            if baseline and stamp > baseline.last_poll:
                observed = len(new_ids) / (stamp - baseline.last_poll)
                rate = observed if rate is None else (observed + rate) / 2
            interval = 0.5 * TRANSFER_LIMIT / rate if rate else maximum
            if gap:
                interval = min(interval, baseline.interval) / 2
                alert(db, f"transfer_gap:{coin.id}:{next(iter(rows))}", "transfer_gap",
                      {"transfer_id": next(iter(rows))}, coin.id)
            interval = max(minimum, min(maximum, interval))
            for transfer_id, data in rows.items():
                if transfer_id in linked:
                    continue
                existing = db.get(Transfer, transfer_id)
                if existing is None:
                    if transfer_id not in new_ids:
                        continue
                    db.add(Transfer(**data, gap=gap))
                    db.flush()
                elif gap:
                    existing.gap = True
                db.add(TransferCoin(transfer_id=transfer_id, coin_id=coin.id))
                if transfer_id in new_ids:
                    evaluate(db, "transfer", transfer_id, coin.id, {
                        **data, "time": data["time"].isoformat() + "Z", "gap": gap,
                        "entities": [item for item in (data["from_entity"], data["to_entity"]) if item],
                    })
            if cursor is None:
                cursor = TransferCursor(coin_id=coin.id)
                db.add(cursor)
            cursor.pricing_id = coin.pricing_id
            cursor.ids = list(rows) or (baseline.ids if baseline else [])
            cursor.last_poll, cursor.rate, cursor.interval = stamp, rate, interval
        return stamp + interval

    async def daily(self, job):
        day = self.today()
        with self.store() as db:
            coin = db.scalar(eligible_coins().where(~select(DailyCollection.coin_id).where(
                DailyCollection.coin_id == Coin.id, DailyCollection.day == day).exists()).order_by(Coin.id).limit(1))
        if coin:
            await self.daily_coin(job.account_id, coin, day, job.options["balance_min"])
            return self.clock()
        if self.catalog is not None and await self.catalog.match_next(job):
            return self.clock()
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()
        return min(self.clock() + job.interval_seconds, max(midnight, self.clock()))

    async def daily_coin(self, account_id, coin, day: date, balance_min):
        rows = await self.gate.get(account_id, BALANCE_PATH, balance_params("7d", balance_min, [coin.pricing_id]),
                                   lambda payload: parse_balances(payload, require_totals=True))
        truncated = len(rows) == BALANCE_LIMIT
        with self.store.begin() as db:
            if db.get(DailyCollection, (day, coin.id)) or db.scalar(eligible_coins().where(
                Coin.id == coin.id, Coin.pricing_id == coin.pricing_id)) is None:
                return
            db.add(DailyCollection(day=day, coin_id=coin.id, truncated=truncated))
            if truncated:
                alert(db, f"daily_truncated:{coin.id}:{day}", "daily_truncated", {"date": str(day)}, coin.id)
            previous_day = day - timedelta(days=1)
            previous = {row.entity_id: row for row in db.scalars(select(DailyBalance).where(
                DailyBalance.coin_id == coin.id, DailyBalance.day == previous_day))}
            for row in rows:
                fields = {key: value for key, value in row.items() if not key.startswith("prev_") and key != "token_balances"}
                db.add(DailyBalance(day=day, coin_id=coin.id, **fields))
                old = previous.get(row["entity_id"])
                if old is None or (old.balance_usd == row["balance_usd"] and old.balance_unit == row["balance_unit"]):
                    continue
                usd_change = row["balance_usd"] - old.balance_usd
                unit_change = row["balance_unit"] - old.balance_unit
                evaluate(db, "balance_diff", f"{day}:{row['entity_id']}", coin.id, {
                    "entity_id": row["entity_id"], "entities": [row["entity_id"]], "date": str(day),
                    "usd": usd_change, "balance_unit_change": unit_change,
                    "balance_change_pct": 100 * unit_change / abs(old.balance_unit) if old.balance_unit else None,
                    "direction": "increase" if (unit_change or usd_change) > 0 else "decrease",
                })

    async def snapshot(self, account_id, interval, balance_min, pricing_ids):
        rows = await self.gate.get(account_id, BALANCE_PATH, balance_params(interval, balance_min, pricing_ids), parse_balances)
        with self.store.begin() as db:
            snapshot = Snapshot(interval=interval, balance_min=balance_min, pricing_ids=pricing_ids,
                                truncated=len(rows) == BALANCE_LIMIT)
            db.add(snapshot)
            db.flush()
            db.add_all(SnapshotRow(snapshot_id=snapshot.id, **row) for row in rows)
            return snapshot


def snapshot_diff(before, after):
    old = {row["entity_id"]: row for row in before}
    new = {row["entity_id"]: row for row in after}
    return {"added": [new[key] for key in sorted(new.keys() - old.keys())],
            "removed": [old[key] for key in sorted(old.keys() - new.keys())],
            "changed": [{"before": old[key], "after": new[key]} for key in sorted(old.keys() & new.keys())
                        if old[key] != new[key]]}
