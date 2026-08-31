"""The store, backed by a live Cognee instance.

Cognee is not a database this cassette writes rows into. It is a memory engine
with its own model of the same problem, so the mapping is deliberate rather
than mechanical:

    a review state   ->  a dataset      (memory_proposed / accepted / rejected)
    an entry         ->  a data item in that dataset
    the entry itself ->  external_metadata on the item
    accepting        ->  cognify()      (the graph is built here, and only here)
    recall           ->  recall() over the accepted dataset alone

Two consequences are worth stating, because both are load-bearing.

**The review gate is structural, not a filter.** Only `memory_accepted` is ever
cognified, so unreviewed prose is never in the knowledge graph to be retrieved
in the first place. A filter can be forgotten; an empty graph cannot. This is
why review state is a dataset rather than a field to select on.

**Identity is the cassette's, not Cognee's.** Cognee mints a fresh `data_id`
per row and dedupes rows by content hash, so a revision or a move between
datasets lands on a new row. The contract's `id` therefore lives inside
`external_metadata` and outlives every row that carries it, which is what keeps
a link to an entry valid while its text is revised and its review state moves.
Cognee's own `data_id` is looked up when a call needs one and is never exposed.

A revision is forget-then-add rather than Cognee's `update()`. `update()`
re-cognifies, and a proposed entry must not cost an LLM call before anyone has
agreed to keep it.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from store import Entry

# One dataset per review state. The names are the contract's own vocabulary, so
# what a dataset holds is legible from Cognee's side too.
DATASETS = {
    "proposed": "memory_proposed",
    "accepted": "memory_accepted",
    "rejected": "memory_rejected",
}

# The only dataset a graph is ever built from, and the only one recall reads.
ACCEPTED = DATASETS["accepted"]


# Where Cognee keeps the relational database, the embedded graph, and the
# files it ingests. Cognee's own default is a directory inside its installed
# package, which in a container is a layer that a redeploy discards — the whole
# point of the durable backend, lost to a default. Naming it here makes the
# volume in compose.yaml the thing that holds the memory.
STORAGE_DIR = os.environ.get("COGNEE_STORAGE_DIR", "").strip()


def _configure_environment() -> None:
    """Settings this cassette needs to be true before Cognee is used.

    Both flags are about being a single-tenant embedded engine rather than the
    multi-user service Cognee defaults to. Set with `setdefault`, so a
    deployment that means something different still wins.
    """
    # Cognee 1.x defaults to multi-user access control, which would put every
    # write behind a user and tenant this cassette has no notion of. The
    # cassette's own boundary is the deployment's (see deploy/aws.sh).
    os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    os.environ.setdefault("TELEMETRY_DISABLED", "true")


def document(entry: Entry) -> str:
    """The text Cognee reads.

    The title leads, because it is the one line a human wrote about this
    memory and the strongest signal in the entity extraction that follows.
    Most contract fields are deliberately absent: they travel in
    external_metadata, where they can be read back exactly, rather than being
    re-derived from prose by an LLM.

    The closing provenance line is not decoration. Cognee dedupes ingested
    documents by content hash, so two sessions whose reflections happen to read
    identically — routine, given a client's template pass writes deterministic
    prose — would collapse onto one row and silently merge two distinct
    memories. Naming the session and kind makes each entry its own document,
    and it tells the graph what the memory is about, which is worth saying
    anyway.
    """
    origin = entry.session_id or "an unrecorded session"
    return f"{entry.title}\n\n{entry.body}\n\nA {entry.kind} derived from {origin}."


def _mentions(blob: str, title: str) -> bool:
    """Does retrieved text actually name this entry?

    Whole-token, not raw substring, and never for a blank title. A plain `in`
    test made two entries un-recallable-by-anything-else: one titled "" (a
    whitespace-only tip title survives clean_title) is a substring of every
    string including "", so it came back for every query ahead of real
    matches; and a short one like "T" or "Bug" matches inside unrelated prose
    ("t" is in "unrelated"), so the graph appeared to have retrieved an entry
    it never saw. The lookarounds rather than \\b so a title that starts or
    ends with punctuation still matches.
    """
    needle = title.strip().lower()
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", blob) is not None


def node_set_for(entry: Entry) -> list[str]:
    """Graph tags, so recall can be narrowed without reading every entry."""
    tags = [f"kind:{entry.kind}"]
    tags += [f"session:{s}" for s in entry.sessionIds]
    return tags


class CogneeStore:
    """Durable, and durable in Cognee's terms: entries live in its datasets.

    Every method is async because Cognee's API is, and the cassette's routes
    await it rather than hiding a loop in a thread.
    """

    durable = True
    backend = "cognee"

    def __init__(self) -> None:
        _configure_environment()

        import cognee

        self._cognee = cognee
        if STORAGE_DIR:
            cognee.config.data_root_directory(os.path.join(STORAGE_DIR, "data"))
            cognee.config.system_root_directory(os.path.join(STORAGE_DIR, "system"))

        # Accepting is a human action on a request path; building a graph is an
        # LLM pipeline. Kept apart, and tracked so a caller can be told an
        # entry is still indexing rather than missing.
        self._cognify_task: asyncio.Task | None = None
        self._cognify_pending = False
        self._setup_done = False
        self._setup_lock = asyncio.Lock()

    async def _ready(self) -> None:
        """Create Cognee's tables once, before anything touches them.

        Cognee builds these on its first write pipeline, but a read can come
        first — a review UI listing an empty queue on a fresh deployment — and
        against an uncreated database that raises rather than returning
        nothing. Doing it here means an empty store reads as empty.
        """
        if self._setup_done:
            return
        async with self._setup_lock:
            if self._setup_done:
                return
            from cognee.modules.engine.operations.setup import setup

            await setup()
            self._setup_done = True

    # --- reading ------------------------------------------------------------

    async def _dataset_ids(self) -> dict[str, Any]:
        """Dataset name -> id, for the three this cassette owns.

        Cognee creates a dataset on first write, so a name missing here means
        nothing has been written in that review state yet, which is ordinary.
        """
        await self._ready()
        wanted = set(DATASETS.values())
        return {
            ds.name: ds.id
            for ds in await self._cognee.datasets.list_datasets()
            if ds.name in wanted
        }

    async def _rows(self) -> list[tuple[Any, Any]]:
        """Every row this cassette owns, paired with the dataset id holding it.

        The dataset id is carried along because `forget` needs it, and reading
        it back off the row is what makes a later delete precise.
        """
        rows = []
        for _, dataset_id in (await self._dataset_ids()).items():
            for row in await self._cognee.datasets.list_data(dataset_id):
                rows.append((dataset_id, row))
        return rows

    @staticmethod
    def _entry_of(row: Any) -> Entry | None:
        """Rebuild the contract entry a row was written with.

        Anything without one is not this cassette's row. Returning None rather
        than raising keeps a foreign write in a shared Cognee instance from
        taking down a listing.
        """
        payload = (row.external_metadata or {}).get("entry")
        if not payload:
            return None
        try:
            return Entry.model_validate(payload)
        except Exception:
            return None

    async def all(self) -> list[Entry]:
        return [e for _, row in await self._rows() if (e := self._entry_of(row))]

    async def find(self, session_id: str, kind: str) -> Entry | None:
        for entry in await self.all():
            if entry.session_id == session_id and entry.kind == kind:
                return entry
        return None

    async def get(self, entry_id: str) -> Entry | None:
        for entry in await self.all():
            if entry.id == entry_id:
                return entry
        return None

    async def _locate(self, entry_id: str) -> tuple[Any, Any] | None:
        """The (dataset_id, row) currently holding an entry, in any state."""
        for dataset_id, row in await self._rows():
            entry = self._entry_of(row)
            if entry and entry.id == entry_id:
                return dataset_id, row
        return None

    # --- writing ------------------------------------------------------------

    async def save(self, entry: Entry) -> Entry:
        """Write an entry into the dataset its review state names.

        Every write is forget-then-add, which covers all three cases with one
        path: a revision (same dataset, new prose), a review decision (a
        different dataset), and a first write (nothing to forget). The row
        moves; the entry's `id` does not, because it rides in the metadata.

        The add is compensated rather than left to fail open. `forget` has
        already removed the old row by the time `add` runs, so an add that
        raises would destroy the entry outright — accepting a memory would
        delete it. The previous version goes back in before the error is
        surfaced: a stale entry beats a missing one.
        """
        located = await self._locate(entry.id)
        previous: Entry | None = None
        if located is not None:
            dataset_id, row = located
            previous = self._entry_of(row)
            await self._cognee.forget(data_id=row.id, dataset_id=dataset_id)

        try:
            await self._add(entry)
        except Exception:
            if previous is not None:
                try:
                    await self._add(previous)
                except Exception:  # pragma: no cover - restore is best effort
                    pass
            raise

        if entry.review == "accepted":
            self._schedule_cognify()
        return entry

    async def _add(self, entry: Entry) -> None:
        """One row, written into the dataset its review state names."""
        from cognee.tasks.ingestion.data_item import DataItem

        await self._cognee.add(
            DataItem(
                data=document(entry),
                label=f"{entry.kind}: {entry.title}",
                external_metadata={"entry": entry.model_dump()},
            ),
            dataset_name=DATASETS[entry.review],
            node_set=node_set_for(entry),
        )

    def _schedule_cognify(self) -> None:
        """Build the graph over accepted entries, off the request path.

        Cognify is an LLM pipeline measured in seconds; accepting is a click.
        The entry is listed as accepted the moment it is written and becomes
        recallable when this finishes, which is why /ping reports indexing
        state rather than leaving the gap to be discovered.

        An accept that arrives mid-run raises a flag the running task picks up
        instead of being dropped. Returning early without one meant the second
        of two quick accepts was written to the accepted dataset and never
        cognified: /ping went back to `indexing: false` while that entry stayed
        unrecallable until some later, unrelated accept happened to rebuild the
        graph.
        """
        if self._cognify_task and not self._cognify_task.done():
            self._cognify_pending = True
            return

        async def run() -> None:
            # Clearing the flag before each pass is what makes it safe: a write
            # that landed before the reset is already in the dataset this pass
            # reads, and one that lands after sets the flag again. Nothing
            # awaits between the final check and the return, so no accept can
            # slip in after the task decides to stop but before it is done.
            while True:
                self._cognify_pending = False
                try:
                    await self._cognee.cognify(datasets=[ACCEPTED])
                except Exception:  # pragma: no cover - depends on a live LLM
                    # A failed cognify leaves the entry accepted and un-indexed,
                    # which the next accept retries. Losing the acceptance because
                    # the LLM was down would be the worse failure.
                    pass
                if not self._cognify_pending:
                    return

        self._cognify_task = asyncio.create_task(run())

    @property
    def indexing(self) -> bool:
        return bool(self._cognify_task and not self._cognify_task.done())

    async def delete_kinds_except(self, session_id: str, kinds: set[str]) -> None:
        for dataset_id, row in await self._rows():
            entry = self._entry_of(row)
            if entry and entry.session_id == session_id and entry.kind not in kinds:
                await self._cognee.forget(data_id=row.id, dataset_id=dataset_id)

    async def counts(self) -> dict[str, int]:
        entries = await self.all()
        return {
            "accepted": sum(1 for e in entries if e.review == "accepted"),
            "proposed": sum(1 for e in entries if e.review == "proposed"),
        }

    async def clear(self) -> None:
        for dataset_id, row in await self._rows():
            await self._cognee.forget(data_id=row.id, dataset_id=dataset_id)

    async def close(self) -> None:
        if self._cognify_task and not self._cognify_task.done():
            self._cognify_task.cancel()

    # --- recall -------------------------------------------------------------

    @staticmethod
    def _text_of(result: Any) -> str:
        """The prose a recall result carries, and only that.

        Cognee's result union is not uniform: a graph entry has `text`, while a
        QA entry carries `answer` and `context` instead. The predecessor fell
        back to `str(result)`, which stringifies the whole model — and a QA
        entry's repr contains the caller's own question, so an entry whose
        title appeared anywhere in the query matched as though the graph had
        retrieved it.
        """
        for field in ("text", "answer", "context"):
            value = getattr(result, field, None)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    async def recall(self, query: str, limit: int) -> list[Entry]:
        """Ask the knowledge graph, and answer in the contract's own entries.

        Scoped to the accepted dataset, which is the review gate: the other two
        were never cognified, so there is nothing of theirs in the graph to
        retrieve even by accident.

        Cognee answers with graph context rather than with rows, so the
        entries it points at are recovered by matching the text it returned
        back onto accepted entries. An entry Cognee ranked is returned ahead of
        the rest; when Cognee returns nothing usable, the caller still gets a
        deterministic answer rather than an empty one.
        """
        accepted = [e for e in await self.all() if e.review == "accepted"]
        if not accepted:
            return []

        ranked: list[Entry] = []
        try:
            results = await self._cognee.recall(
                query,
                datasets=[ACCEPTED],
                top_k=limit,
                only_context=True,
            )
            blob = " ".join(self._text_of(r) for r in results).strip().lower()
            ranked = [e for e in accepted if blob and _mentions(blob, e.title)]
        except Exception:  # pragma: no cover - depends on a live LLM
            ranked = []

        # What the graph did not answer for still has to be answered honestly.
        # Falling back to every accepted entry by confidence would hand a query
        # about one thing a page about another and call it recall; the same
        # substring match the volatile store uses is worth less than a graph
        # and is at least about what was asked.
        seen = {e.id for e in ranked}
        needle = query.lower()
        rest = sorted(
            (
                e
                for e in accepted
                if e.id not in seen
                and (needle in e.title.lower() or needle in e.body.lower())
            ),
            key=lambda e: e.confidence,
            reverse=True,
        )
        return (ranked + rest)[:limit]
