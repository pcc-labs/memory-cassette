"""The memory cassette: an independently deployed HTTP service tapes admits.

It never imports tapes. Core fetches one URL (`/openapi`), reads the manifest
riding inside it as a root extension, validates prefix containment, rewrites
every path under /v1/cassettes/<name>/, and proxies. That is the whole
coupling, which is why this is Python next to a Go core.

Three admission rules shape this file:

  1. Every path in the document must sit below the cassette's local API prefix
     (/api/<name>), or core refuses the whole document.
  2. The health and OpenAPI anchors must NOT appear as operations, hence
     include_in_schema=False on both.
  3. Every operation needs at least one response, and operation IDs must be
     unique within the cassette.

Persistence lives in store.py. Without TAPES_DATABASE_URL this runs on a
volatile in-process store and says so at /ping, which is fine for a look
around and wrong for anything hosted.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from manifest import VERSION, manifest
from store import Entry as MemoryEntry
from store import MemoryKind, MemoryReview, MemoryStatus, open_store

NAME = os.environ.get("CASSETTE_NAME", "memory")
PREFIX = f"/api/{NAME}"

store = open_store(os.environ.get("TAPES_DATABASE_URL", ""))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    parts = [p for p in "".join(keep).split("-") if p]
    return "-".join(parts)[:60] or "entry"


class MemoryCounts(BaseModel):
    accepted: int
    proposed: int


class MemoryPage(BaseModel):
    items: list[MemoryEntry]
    counts: MemoryCounts


class ReviewRequest(BaseModel):
    review: MemoryReview


class DreamTip(BaseModel):
    id: str
    title: str
    body: str


class DreamIngest(BaseModel):
    """The output of paperplane's `dreamOnSession`, posted verbatim."""

    sessionId: str
    observations: list[str] = Field(default_factory=list)
    reflection: str | None = None
    tip: DreamTip | None = None


class RecallRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)


class RecallResponse(BaseModel):
    items: list[MemoryEntry]
    query: str


app = FastAPI(
    title="Memory Cassette",
    version=VERSION,
    description="Derived agent memory over tapes sessions.",
    # Core fetches /openapi, not /openapi.json, and the anchors must not be
    # operations. Both defaults are turned off and re-served below.
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)


@app.get("/ping", include_in_schema=False)
def ping() -> Response:
    # `store` is the load-bearing field: on the volatile backend every memory
    # dies with the process, and that should be visible from the health check
    # rather than discovered after a restart.
    return JSONResponse(
        {
            "status": "ok",
            "cassette": NAME,
            "store": "postgres" if store.durable else "memory",
            "durable": store.durable,
        }
    )


@app.get("/openapi", include_in_schema=False)
def openapi_document() -> Response:
    doc = app.openapi()
    # Must stay OpenAPI 3.1. Core gates the MCP bridge on it:
    # api/cassetterunner/mcp.go refuses `x-tapes-mcp` with
    # "requires OpenAPI 3.1 JSON Schema" on anything else, and refuses the
    # whole document with it. FastAPI emits 3.1 natively, so do not override
    # the version here. (The hello-world example compiles to 3.0 because it
    # declares no MCP tools; that is its choice, not a core requirement.)
    doc["x-tapes-cassette"] = manifest(NAME)
    return JSONResponse(doc)


@app.get(
    f"{PREFIX}/entries",
    operation_id="listMemoryEntries",
    summary="List memory entries, filtered by review state",
    response_model=MemoryPage,
    tags=[NAME],
)
def list_entries(
    review: MemoryReview = "accepted",
    kind: MemoryKind | None = None,
    status: MemoryStatus | None = None,
    q: str | None = None,
) -> MemoryPage:
    items = [e for e in store.all() if e.review == review]
    if kind:
        items = [e for e in items if e.kind == kind]
    if status:
        items = [e for e in items if e.status == status]
    if q:
        needle = q.lower()
        items = [e for e in items if needle in e.title.lower() or needle in e.body.lower()]
    items.sort(key=lambda e: e.lastSeenAt, reverse=True)
    return MemoryPage(items=items, counts=MemoryCounts(**store.counts()))


@app.get(
    f"{PREFIX}/entries/{{entry_id}}",
    operation_id="getMemoryEntry",
    summary="Read one memory entry",
    response_model=MemoryEntry,
    tags=[NAME],
)
def get_entry(entry_id: str) -> MemoryEntry:
    found = store.get(entry_id)
    if not found:
        raise HTTPException(status_code=404, detail="not found")
    return found


@app.post(
    f"{PREFIX}/entries/{{entry_id}}/review",
    operation_id="reviewMemoryEntry",
    summary="Accept or reject an entry",
    response_model=MemoryEntry,
    tags=[NAME],
)
def review_entry(entry_id: str, body: ReviewRequest) -> MemoryEntry:
    """The review gate is the safety property: only accepted entries leave the
    review queue, and only accepted entries are ever injected into agent
    sessions."""
    found = store.get(entry_id)
    if not found:
        raise HTTPException(status_code=404, detail="not found")
    return store.save(found.model_copy(update={"review": body.review, "lastSeenAt": now_iso()}))


def upsert(
    session_id: str, kind: MemoryKind, title: str, text: str, confidence: float, attrs: dict, stamp: str
) -> MemoryEntry:
    """Write one memory for a session, superseding what that session said before.

    Paperplane reflects the same session more than once: a deterministic
    template pass on page load, then an LLM upgrade seconds later, plus another
    pair on every manual regenerate. Those are revisions of one judgment, not
    separate memories, so they collapse onto one entry per (session, kind).

    The store keeps identity stable across a revision, so `id` and `firstSeenAt`
    survive and a link to an entry stays valid while its text changes.
    """
    existing = store.find(session_id, kind)
    if existing is not None and existing.title == title and existing.body == text:
        # Re-opening a session page re-fires the reflection. Identical content
        # is not new information, so an acceptance already granted still stands.
        return store.save(existing.model_copy(update={"lastSeenAt": stamp}))

    return store.save(
        MemoryEntry(
            id=existing.id if existing else str(uuid.uuid4()),
            kind=kind,
            slug=slugify(title),
            title=title,
            body=text,
            status="open",
            # The gate promises that only text a human accepted is recallable.
            # New prose under an old acceptance would break that quietly, so a
            # revision goes back for review.
            review="proposed",
            confidence=confidence,
            occurrenceCount=1,
            sessionIds=[session_id],
            attrs=attrs,
            firstSeenAt=existing.firstSeenAt if existing else stamp,
            lastSeenAt=stamp,
        )
    )


@app.post(
    f"{PREFIX}/ingest/dream",
    operation_id="ingestDream",
    summary="Ingest a paperplane reflection and dream as memory entries",
    response_model=list[MemoryEntry],
    tags=[NAME],
)
def ingest_dream(body: DreamIngest) -> list[MemoryEntry]:
    """Paperplane's dream output maps onto the console's existing kind enum
    with nothing left over: a reflection is an `observation`, a tip is a `tip`.
    Both land as `proposed` and wait for the review gate."""
    stamp = now_iso()

    incoming: list[tuple[MemoryKind, str, str, float, dict[str, Any]]] = []
    if body.reflection:
        incoming.append(
            (
                "observation",
                body.reflection[:80],
                body.reflection,
                0.6,
                {"source": "paperplane.reflection", "observations": body.observations},
            )
        )
    if body.tip:
        incoming.append(
            (
                "tip",
                body.tip.title,
                body.tip.body,
                0.7,
                {"source": "paperplane.dream", "ruleId": body.tip.id},
            )
        )

    written = [upsert(body.sessionId, *fields, stamp) for fields in incoming]

    # A later pass can come back without a tip. The old tip was derived from a
    # reflection that no longer stands, so it does not outlive it.
    store.delete_kinds_except(body.sessionId, {kind for kind, *_ in incoming})

    return written


@app.post(
    f"{PREFIX}/recall",
    operation_id="recallMemory",
    summary="Recall accepted memory relevant to a query",
    response_model=RecallResponse,
    tags=[NAME],
    # Publishes this operation to agents as the MCP tool `memory.recall`.
    # The bridge is narrow and this operation is built to fit it: POST only,
    # an inline required JSON object body, a JSON object response, and no
    # path, query, header, or cookie parameters.
    openapi_extra={
        "x-tapes-mcp": {
            "name": "recall",
            "annotations": {"readOnlyHint": True},
        }
    },
)
def recall(body: RecallRequest) -> RecallResponse:
    """Only accepted entries are recallable. Cognee's GRAPH_COMPLETION replaces
    the substring match here without changing the signature."""
    needle = body.query.lower()
    items = [
        e
        for e in store.all()
        if e.review == "accepted"
        and (needle in e.title.lower() or needle in e.body.lower())
    ]
    items.sort(key=lambda e: e.confidence, reverse=True)
    return RecallResponse(items=items[: body.limit], query=body.query)
