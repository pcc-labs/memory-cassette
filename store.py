"""Where memories live.

Two backends behind one interface. Postgres is the real one; the in-memory
fallback exists so the cassette still starts, and still runs its tests, without
a database credential. That fallback is the same shape the `hello-world`
example uses, and like that example the cassette says which one answered.

The split matters more than it looks. A hosted memory service that forgets on
restart is not a memory service, so anything long-lived must run on Postgres.
`durable` is what lets the API say so out loud instead of failing quietly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryKind = Literal[
    "bug", "decision", "gotcha", "preference", "todo", "tip", "observation", "anomaly"
]
MemoryStatus = Literal["open", "resolved"]
MemoryReview = Literal["proposed", "accepted", "rejected"]


class Entry(BaseModel):
    """One derived memory. Field names mirror the `/v1/memory` client
    contract so an existing review UI works unchanged."""

    id: str
    kind: MemoryKind
    slug: str
    title: str
    body: str
    status: MemoryStatus = "open"
    review: MemoryReview = "proposed"
    confidence: float = Field(ge=0, le=1, default=0.5)
    occurrenceCount: int = Field(ge=1, default=1)
    sessionIds: list[str] = Field(default_factory=list)
    attrs: dict[str, Any] = Field(default_factory=dict)
    firstSeenAt: str
    lastSeenAt: str

    @property
    def session_id(self) -> str:
        return self.sessionIds[0] if self.sessionIds else ""


class MemoryStore:
    """Process-local. Everything written here dies with the process."""

    durable = False

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Entry] = {}

    def migrate(self) -> None:  # nothing to build
        pass

    def close(self) -> None:
        pass

    def clear(self) -> None:
        self._rows.clear()

    def all(self) -> list[Entry]:
        return list(self._rows.values())

    def find(self, session_id: str, kind: str) -> Entry | None:
        return self._rows.get((session_id, kind))

    def get(self, entry_id: str) -> Entry | None:
        return next((e for e in self._rows.values() if e.id == entry_id), None)

    def save(self, entry: Entry) -> Entry:
        key = (entry.session_id, entry.kind)
        # Identity is stable across a revision: a client links to an entry by
        # id, so a re-save under an existing key keeps the id it was given.
        existing = self._rows.get(key)
        if existing is not None:
            entry = entry.model_copy(update={"id": existing.id})
        self._rows[key] = entry
        return entry

    def delete_kinds_except(self, session_id: str, kinds: set[str]) -> None:
        for key in [k for k in self._rows if k[0] == session_id and k[1] not in kinds]:
            del self._rows[key]

    def counts(self) -> dict[str, int]:
        rows = self._rows.values()
        return {
            "accepted": sum(1 for e in rows if e.review == "accepted"),
            "proposed": sum(1 for e in rows if e.review == "proposed"),
        }


SCHEMA = "memory"

# (session_id, kind) is the primary key rather than an index, so "one memory per
# session per kind" is a property of the database instead of a rule the
# application has to keep remembering. `id` is carried alongside and never
# rewritten by an upsert, which is what makes a link to an entry stay valid
# while its text is revised.
DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.entries (
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    id         TEXT NOT NULL UNIQUE,
    review     TEXT NOT NULL,
    entry      JSONB NOT NULL,
    PRIMARY KEY (session_id, kind)
);
CREATE INDEX IF NOT EXISTS entries_review_idx ON {SCHEMA}.entries (review);
"""


class PostgresStore:
    """Durable. The cassette owns this schema and migrates it itself; tapes
    core creates nothing and never holds this credential."""

    durable = True

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True)

    def migrate(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(DDL)

    def close(self) -> None:
        self._conn.close()

    def clear(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f"TRUNCATE {SCHEMA}.entries")

    def all(self) -> list[Entry]:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT entry FROM {SCHEMA}.entries")
            return [Entry.model_validate(row[0]) for row in cur.fetchall()]

    def find(self, session_id: str, kind: str) -> Entry | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT entry FROM {SCHEMA}.entries WHERE session_id = %s AND kind = %s",
                (session_id, kind),
            )
            row = cur.fetchone()
        return Entry.model_validate(row[0]) if row else None

    def get(self, entry_id: str) -> Entry | None:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT entry FROM {SCHEMA}.entries WHERE id = %s", (entry_id,))
            row = cur.fetchone()
        return Entry.model_validate(row[0]) if row else None

    def save(self, entry: Entry) -> Entry:
        from psycopg.types.json import Jsonb

        # DO UPDATE deliberately omits `id`: an upsert revises an entry's text,
        # never its identity. The RETURNING tells us which id actually won, so
        # the caller sees the stored entry rather than the one it proposed.
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {SCHEMA}.entries (session_id, kind, id, review, entry)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (session_id, kind) DO UPDATE
                    SET review = EXCLUDED.review,
                        entry  = jsonb_set(EXCLUDED.entry, '{{id}}',
                                           to_jsonb({SCHEMA}.entries.id))
                RETURNING entry
                """,
                (entry.session_id, entry.kind, entry.id, entry.review, Jsonb(entry.model_dump())),
            )
            return Entry.model_validate(cur.fetchone()[0])

    def delete_kinds_except(self, session_id: str, kinds: set[str]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {SCHEMA}.entries WHERE session_id = %s AND NOT (kind = ANY(%s))",
                (session_id, list(kinds)),
            )

    def counts(self) -> dict[str, int]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT review, count(*) FROM {SCHEMA}.entries "
                "WHERE review IN ('accepted', 'proposed') GROUP BY review"
            )
            tally = dict(cur.fetchall())
        return {"accepted": tally.get("accepted", 0), "proposed": tally.get("proposed", 0)}


def open_store(dsn: str):
    """Postgres when a credential is supplied, otherwise the volatile fallback.

    Deliberately not an error. A cassette that refuses to boot without a
    database is harder to try out, and the API reports which backend answered
    so "it forgot everything" is never a mystery.
    """
    if not dsn:
        return MemoryStore()
    store = PostgresStore(dsn)
    store.migrate()
    return store
