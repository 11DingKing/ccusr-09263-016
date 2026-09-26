"""SQLite 存储实现：单连接 + 可重入事务，供服务重启后恢复状态。"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    collection TEXT NOT NULL,
    key        TEXT NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (collection, key)
);
"""


class SQLiteStore:
    """以 SQLite 为后端的文档存储。

    - 写事务使用 ``BEGIN IMMEDIATE``，多线程/多进程下串行化写者；
    - ``transaction`` 可重入，仅最外层提交/回滚；
    - 运行数据落在调用方给定的目录（不得写入源码目录）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path != Path(":memory:"):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # 排他锁贯穿整个事务：同一线程可重入，其他线程阻塞至外层事务
        # 提交/回滚，与 SQLite 单写者语义一致，保证并发锁定串行化。
        self._lock.acquire()
        try:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.rollback()
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._conn.commit()
        finally:
            self._lock.release()

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM records WHERE collection = ? AND key = ?", (collection, key)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def put(self, collection: str, key: str, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO records (collection, key, data) VALUES (?, ?, ?) "
                "ON CONFLICT (collection, key) DO UPDATE SET data = excluded.data",
                (collection, key, payload),
            )

    def delete(self, collection: str, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM records WHERE collection = ? AND key = ?", (collection, key))

    def query(self, collection: str, **filters: Any) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM records WHERE collection = ?", (collection,)).fetchall()
        records = [json.loads(row["data"]) for row in rows]
        return [r for r in records if all(r.get(field) == value for field, value in filters.items())]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
