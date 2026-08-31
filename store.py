"""Where memories live.

Two backends behind one interface. Cognee is the real one; the in-memory
fallback exists so the cassette still starts, and still runs its tests, without
a Cognee environment or an LLM credential. That fallback is the same shape the
`hello-world` example uses, and like that example the cassette says which one
answered.

The split matters more than it looks. A hosted memory service that forgets on
restart is not a memory service, so anything long-lived must run on Cognee.
`durable` is what lets the API say so out loud instead of failing quietly.

Both backends are async, because Cognee's API is and the routes await it
directly rather than hiding an event loop in a thread.
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
    backend = "memory"
    indexing = False

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Entry] = {}

    async def close(self) -> None:
        pass

    async def clear(self) -> None:
        self._rows.clear()

    async def all(self) -> list[Entry]:
        return list(self._rows.values())

    async def find(self, session_id: str, kind: str) -> Entry | None:
        return self._rows.get((session_id, kind))

    async def get(self, entry_id: str) -> Entry | None:
        return next((e for e in self._rows.values() if e.id == entry_id), None)

    async def save(self, entry: Entry) -> Entry:
        key = (entry.session_id, entry.kind)
        # Identity is stable across a revision: a client links to an entry by
        # id, so a re-save under an existing key keeps the id it was given.
        existing = self._rows.get(key)
        if existing is not None:
            entry = entry.model_copy(update={"id": existing.id})
        self._rows[key] = entry
        return entry

    async def delete_kinds_except(self, session_id: str, kinds: set[str]) -> None:
        for key in [k for k in self._rows if k[0] == session_id and k[1] not in kinds]:
            del self._rows[key]

    async def counts(self) -> dict[str, int]:
        rows = self._rows.values()
        return {
            "accepted": sum(1 for e in rows if e.review == "accepted"),
            "proposed": sum(1 for e in rows if e.review == "proposed"),
        }

    async def recall(self, query: str, limit: int) -> list[Entry]:
        """Substring matching, which is all a store with no graph can offer.

        The signature is the one CogneeStore implements against a knowledge
        graph, so the route does not know which backend answered it.
        """
        needle = query.lower()
        items = [
            e
            for e in await self.all()
            if e.review == "accepted"
            and (needle in e.title.lower() or needle in e.body.lower())
        ]
        items.sort(key=lambda e: e.confidence, reverse=True)
        return items[:limit]


def open_store(enabled: bool):
    """Cognee when the deployment asked for it, otherwise the volatile fallback.

    Deliberately not automatic. Cognee is a heavy engine with an LLM credential
    behind it, so a deployment says outright that it wants one
    (COGNEE_ENABLED) rather than having a memory service quietly start
    ingesting against whatever happens to be configured.

    Asking for Cognee and not getting it is a startup failure, not a fallback.
    A deployment that said COGNEE_ENABLED=true wants durable memory, and
    quietly answering with the volatile store instead would lose every entry
    on the next restart while /ping cheerfully reported a healthy service —
    the failure this whole module is arranged to make impossible. The fallback
    is for deployments that did not ask, and the API reports which backend
    answered so "it forgot everything" is never a mystery.
    """
    if not enabled:
        return MemoryStore()

    from cognee_store import CogneeStore

    return CogneeStore()
