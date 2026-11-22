import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Event, Rule, RuleMatch, now


def alert(db: Session, key: str, kind: str, data: dict, coin_id: str | None = None):
    if db.scalar(select(Event.id).where(Event.dedupe_key == key)) is None:
        db.add(Event(dedupe_key=key, kind=kind, coin_id=coin_id, level="important", push=True, data=data))


def matches(conditions: dict, coin_id: str, data: dict) -> bool:
    entities = set(data.get("entities", ()))
    if conditions.get("coins_include") and coin_id not in conditions["coins_include"]:
        return False
    if coin_id in (conditions.get("coins_exclude") or ()):
        return False
    if conditions.get("entities_include") and not entities.intersection(conditions["entities_include"]):
        return False
    if entities.intersection(conditions.get("entities_exclude") or ()):
        return False
    if conditions.get("direction") and conditions["direction"] != data.get("direction"):
        return False
    for condition, field in (("min_usd", "usd"), ("min_balance_change_pct", "balance_change_pct")):
        threshold = conditions.get(condition)
        if threshold is not None and (data.get(field) is None or abs(data[field]) < threshold):
            return False
    return True


def evaluate(db: Session, source: str, source_id: str, coin_id: str, data: dict):
    stamp = now()
    for rule in db.scalars(select(Rule).where(Rule.source == source, Rule.enabled)):
        if not matches(rule.conditions, coin_id, data):
            continue
        source_key = json.dumps([coin_id, source_id])
        if db.get(RuleMatch, (rule.id, source_key)):
            continue
        db.add(RuleMatch(rule_id=rule.id, source_key=source_key))
        group = json.dumps([rule.id, coin_id, data.get("direction"), sorted(set(data.get("entities", ())))])
        previous = db.scalar(select(Event).where(Event.group_key == group).order_by(Event.id.desc()).limit(1))
        if previous and rule.merge_seconds and stamp - previous.created_at < timedelta(seconds=rule.merge_seconds):
            previous.count += 1
            previous.last_at = stamp
            previous.data = data
            continue
        if previous and rule.cooldown_seconds and stamp - previous.created_at < timedelta(seconds=rule.cooldown_seconds):
            continue
        db.add(Event(dedupe_key=f"rule:{rule.id}:{source_key}", kind=source, rule_id=rule.id, coin_id=coin_id,
                     group_key=group, level=rule.level, push=rule.push, created_at=stamp, last_at=stamp, data=data))
