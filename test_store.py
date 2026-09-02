"""The store contract, run against every backend.

The in-memory backend always runs. The Cognee backend runs too when
COGNEE_ENABLED is set (`make test-cognee`), because the whole point of the
Cognee backend is a property the in-memory one cannot have: entries outliving
the process, in a knowledge graph rather than a dict.

The Cognee cases need the extra installed (`uv sync --extra cognee`) and write
into whatever Cognee environment is configured, so they are opt-in rather than
skipped-by-default noise.
"""

import os

import pytest

from store import Entry, MemoryStore, open_store

COGNEE_ENABLED = os.environ.get("COGNEE_ENABLED", "").lower() in {"1", "true", "yes"}


def entry(
    session_id="sess_1",
    kind="observation",
    title="T",
    body="B",
    review="proposed",
):
    return Entry(
        id=f"{session_id}:{kind}",
        kind=kind,
        slug="t",
        title=title,
        body=body,
        status="open",
        review=review,
        confidence=0.5,
        occurrenceCount=1,
        sessionIds=[session_id],
        attrs={"source": "test"},
        firstSeenAt="2026-01-01T00:00:00+00:00",
        lastSeenAt="2026-01-01T00:00:00+00:00",
    )


def make_memory():
    return MemoryStore()


def make_cognee():
    from cognee_store import CogneeStore

    return CogneeStore()


BACKENDS = [pytest.param(make_memory, id="memory")]
if COGNEE_ENABLED:
    BACKENDS.append(pytest.param(make_cognee, id="cognee"))
else:
    BACKENDS.append(
        pytest.param(
            make_cognee,
            id="cognee",
            marks=pytest.mark.skip(
                reason="COGNEE_ENABLED not set; run `make test-cognee`"
            ),
        )
    )


@pytest.fixture(params=BACKENDS)
async def store(request):
    s = request.param()
    await s.clear()
    yield s
    await s.clear()


async def test_saving_then_finding_round_trips(store):
    await store.save(entry())
    found = await store.find("sess_1", "observation")
    assert found is not None
    assert found.title == "T"
    assert found.sessionIds == ["sess_1"]
    assert found.attrs == {"source": "test"}


async def test_find_is_scoped_to_both_session_and_kind(store):
    await store.save(entry(session_id="sess_1", kind="observation"))
    await store.save(entry(session_id="sess_1", kind="tip"))
    await store.save(entry(session_id="sess_2", kind="observation"))

    assert (await store.find("sess_1", "tip")).kind == "tip"
    assert await store.find("sess_2", "tip") is None
    assert len(await store.all()) == 3


async def test_saving_the_same_key_twice_replaces_rather_than_duplicates(store):
    await store.save(entry(title="First"))
    await store.save(entry(title="Second"))
    assert len(await store.all()) == 1
    assert (await store.find("sess_1", "observation")).title == "Second"


async def test_get_by_id_finds_what_save_wrote(store):
    saved = entry()
    await store.save(saved)
    assert (await store.get(saved.id)).title == "T"
    assert await store.get("nope") is None


async def test_counts_only_tally_the_two_review_states_a_review_ui_shows(store):
    await store.save(entry(session_id="a", review="accepted"))
    await store.save(entry(session_id="b", review="proposed"))
    await store.save(entry(session_id="c", review="rejected"))
    assert await store.counts() == {"accepted": 1, "proposed": 1}


async def test_delete_kinds_except_clears_what_a_later_pass_dropped(store):
    await store.save(entry(kind="observation"))
    await store.save(entry(kind="tip"))
    await store.delete_kinds_except("sess_1", {"observation"})
    assert await store.find("sess_1", "tip") is None
    assert await store.find("sess_1", "observation") is not None


async def test_delete_kinds_except_leaves_other_sessions_alone(store):
    await store.save(entry(session_id="sess_1", kind="tip"))
    await store.save(entry(session_id="sess_2", kind="tip"))
    await store.delete_kinds_except("sess_1", set())
    assert await store.find("sess_2", "tip") is not None


async def test_delete_removes_one_entry_whatever_its_state(store):
    await store.save(entry(session_id="a", review="accepted"))
    await store.save(entry(session_id="b", review="proposed"))
    assert await store.delete("a:observation") is True
    assert await store.get("a:observation") is None
    assert await store.find("a", "observation") is None
    assert await store.get("b:observation") is not None
    assert await store.counts() == {"accepted": 0, "proposed": 1}
    assert await store.delete("a:observation") is False


async def test_recall_returns_only_accepted_entries(store):
    """The gate, at the level the store can enforce it. On Cognee this is
    structural (only the accepted dataset is ever cognified); here it is the
    property both backends must agree on."""
    await store.save(entry(session_id="a", title="Kept", review="accepted"))
    await store.save(entry(session_id="b", title="Kept", review="proposed"))
    await store.save(entry(session_id="c", title="Kept", review="rejected"))

    got = await store.recall("Kept", limit=10)
    assert [e.session_id for e in got] == ["a"]


async def test_recall_does_not_answer_with_whatever_it_has(store):
    """A query about one thing must not come back with a page about another.

    Worth pinning on both backends: the Cognee one asks a knowledge graph and
    has to do something when the graph answers nothing, and "return every
    accepted entry by confidence" is the tempting wrong answer.
    """
    await store.save(entry(session_id="a", title="Blackout", review="accepted"))

    assert await store.recall("something else entirely", limit=10) == []


# --- the property the whole change exists for --------------------------------


@pytest.mark.skipif(
    not COGNEE_ENABLED, reason="COGNEE_ENABLED not set; run `make test-cognee`"
)
async def test_entries_outlive_the_process():
    """A hosted memory service that forgets on restart is not a memory service.
    A second store object against the same Cognee environment stands in for a
    redeploy."""
    from cognee_store import CogneeStore

    first = CogneeStore()
    await first.clear()
    await first.save(entry(title="Written before the restart"))
    await first.close()

    second = CogneeStore()
    try:
        survived = await second.find("sess_1", "observation")
        assert survived is not None, "the entry did not survive"
        assert survived.title == "Written before the restart"
    finally:
        await second.clear()
        await second.close()


@pytest.mark.skipif(
    not COGNEE_ENABLED, reason="COGNEE_ENABLED not set; run `make test-cognee`"
)
async def test_a_review_decision_keeps_the_entrys_identity():
    """Cognee mints a fresh row id per write and rows are dataset-scoped, so
    accepting an entry lands it on a different row. The contract's id rides in
    external_metadata precisely so a link to an entry survives that move."""
    from cognee_store import CogneeStore

    store = CogneeStore()
    await store.clear()
    try:
        written = await store.save(entry(title="Proposed prose"))
        accepted = await store.save(written.model_copy(update={"review": "accepted"}))

        assert accepted.id == written.id
        assert len(await store.all()) == 1
        assert (await store.get(written.id)).review == "accepted"
    finally:
        await store.clear()
        await store.close()


def test_open_store_falls_back_to_memory_when_cognee_is_not_asked_for():
    """Same fallback the hello-world example uses: no Cognee means the cassette
    still runs, it just does not remember."""
    store = open_store(False)
    assert isinstance(store, MemoryStore)
    assert store.durable is False
    assert store.backend == "memory"


# --- the two failure modes the fixes exist for -------------------------------


@pytest.mark.skipif(
    not COGNEE_ENABLED, reason="COGNEE_ENABLED not set; run `make test-cognee`"
)
async def test_a_transient_write_failure_leaves_the_previous_version():
    """save() forgets the old row before adding the new one, so an add that
    raises used to destroy the entry outright — accepting a memory deleted it.
    A stale entry beats a missing one.

    Transient specifically: the write of the new text fails, the restore of the
    old text succeeds. See the test below for what this does not cover.
    """
    from cognee_store import CogneeStore

    store = CogneeStore()
    await store.clear()
    try:
        written = await store.save(entry(title="Original prose"))

        original_add = store._add

        async def fail_only_the_new_version(e):
            if e.title == "Revised":
                raise RuntimeError("db blip")
            await original_add(e)

        store._add = fail_only_the_new_version
        try:
            with pytest.raises(RuntimeError):
                await store.save(written.model_copy(update={"title": "Revised"}))
        finally:
            store._add = original_add

        survived = await store.get(written.id)
        assert survived is not None, "the entry was destroyed by a failed write"
        assert survived.title == "Original prose"
    finally:
        await store.clear()
        await store.close()


@pytest.mark.skipif(
    not COGNEE_ENABLED, reason="COGNEE_ENABLED not set; run `make test-cognee`"
)
async def test_a_sustained_outage_still_destroys_the_entry():
    """The compensation is best effort, and this is the gap it does not close.

    The restore is itself a write, so when the engine is down rather than
    momentarily unhappy — the OOM the instance type was raised for, a database
    that has gone away — it fails too and the entry is lost. Pinned rather than
    fixed: closing it means writing the replacement before forgetting the old
    row, which is a change to how save() orders its work, not another catch.
    """
    from cognee_store import CogneeStore

    store = CogneeStore()
    await store.clear()
    try:
        written = await store.save(entry(title="Original prose"))

        async def always_fail(e):
            raise RuntimeError("engine is down")

        store._add = always_fail
        with pytest.raises(RuntimeError):
            await store.save(written.model_copy(update={"title": "Revised"}))

        assert await store.get(written.id) is None, (
            "if this now passes, save() was made durable and this test should "
            "become the opposite assertion"
        )
    finally:
        await store.clear()
        await store.close()


@pytest.mark.skipif(
    not COGNEE_ENABLED, reason="COGNEE_ENABLED not set; run `make test-cognee`"
)
async def test_an_accept_during_an_in_flight_cognify_is_not_dropped():
    """The second of two quick accepts used to land in the accepted dataset and
    never be cognified, while /ping went back to indexing:false over it."""
    import asyncio

    from cognee_store import CogneeStore

    store = CogneeStore()
    await store.clear()
    passes = 0

    async def fake_cognify(datasets=None):
        nonlocal passes
        passes += 1
        await asyncio.sleep(0.2)

    store._cognee = type(
        "Stub", (), {"cognify": staticmethod(fake_cognify), "__getattr__": None}
    )()
    try:
        store._schedule_cognify()
        await asyncio.sleep(0.05)  # first pass is running
        store._schedule_cognify()  # arrives mid-flight
        assert store.indexing is True

        for _ in range(50):
            if not store.indexing:
                break
            await asyncio.sleep(0.1)

        assert passes == 2, f"the second accept was dropped (passes={passes})"
    finally:
        await store.close()
