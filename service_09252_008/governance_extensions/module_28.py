"""可审计的业务边界扩展 28。"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from hashlib import sha256
import json

@dataclass(frozen=True)
class Entry:
    key: str
    owner: str
    state: str
    amount: int
    version: int = 1
    reason: str = "created"

    def digest(self) -> str:
        return sha256(json.dumps(asdict(self), ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def change(self, state: str, amount: int, reason: str) -> "Entry":
        if amount < 0 or not reason.strip():
            raise ValueError("invalid change")
        return Entry(self.key, self.owner, state, amount, self.version + 1, reason)

class Ledger:
    def __init__(self):
        self.rows = {}
        self.history = {}

    def create(self, key: str, owner: str, amount: int) -> Entry:
        if key in self.rows:
            raise ValueError("duplicate key")
        entry = Entry(key, owner, "open", amount)
        self.rows[key] = entry
        self.history[key] = [entry]
        return entry

    def update(self, key: str, state: str, amount: int | None = None, reason: str = "update") -> Entry:
        old = self.rows[key]
        entry = old.change(state, old.amount if amount is None else amount, reason)
        self.rows[key] = entry
        self.history[key].append(entry)
        return entry

    def get(self, key: str) -> Entry | None:
        return self.rows.get(key)

    def verify(self) -> bool:
        return all(len({x.digest() for x in values}) == len(values) for values in self.history.values())

    def snapshot(self) -> list[dict]:
        return [asdict(self.rows[key]) for key in sorted(self.rows)]
