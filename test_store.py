"""The store contract, run against every backend.

The in-memory backend always runs. The Postgres backend runs too when
TAPES_DATABASE_URL is set (`make test-pg` brings one up), because the whole
point of the Postgres backend is a property the in-memory one cannot have:
entries outliving the process.
"""

import os

import pytest

from store import MemoryStore, PostgresStore, open_store

DATABASE_URL = os.environ.get("TAPES_DATABASE_URL", "")


def entry(
    store_mod,
    session_id="sess_1",
    kind="observation",
    title="T",
    body="B",
    review="proposed",
):
    return store_mod.Entry(
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


def make_postgres():
    store = PostgresStore(DATABASE_URL)
    store.migrate()
    store.clear()
    return store


BACKENDS = [pytest.param(make_memory, id="memory")]
if DATABASE_URL:
    BACKENDS.append(pytest.param(make_postgres, id="postgres"))
else:
    BACKENDS.append(
        pytest.param(
            make_postgres,
            id="postgres",
            marks=pytest.mark.skip(
                reason="TAPES_DATABASE_URL not set; run `make test-pg`"
            ),
        )
    )


@pytest.fixture(params=BACKENDS)
def store(request):

    s = request.param()
    s.clear()
    yield s
    s.clear()


@pytest.fixture
def mod():
    import store as store_mod

    return store_mod


def test_saving_then_finding_round_trips(store, mod):
    store.save(entry(mod))
    found = store.find("sess_1", "observation")
    assert found is not None
    assert found.title == "T"
    assert found.sessionIds == ["sess_1"]
    assert found.attrs == {"source": "test"}


def test_find_is_scoped_to_both_session_and_kind(store, mod):
    store.save(entry(mod, session_id="sess_1", kind="observation"))
    store.save(entry(mod, session_id="sess_1", kind="tip"))
    store.save(entry(mod, session_id="sess_2", kind="observation"))

    assert store.find("sess_1", "tip").kind == "tip"
    assert store.find("sess_2", "tip") is None
    assert len(store.all()) == 3


def test_saving_the_same_key_twice_replaces_rather_than_duplicates(store, mod):
    store.save(entry(mod, title="First"))
    store.save(entry(mod, title="Second"))
    assert len(store.all()) == 1
    assert store.find("sess_1", "observation").title == "Second"


def test_get_by_id_finds_what_save_wrote(store, mod):
    saved = entry(mod)
    store.save(saved)
    assert store.get(saved.id).title == "T"
    assert store.get("nope") is None


def test_counts_only_tally_the_two_review_states_a_review_ui_shows(store, mod):
    store.save(entry(mod, session_id="a", review="accepted"))
    store.save(entry(mod, session_id="b", review="proposed"))
    store.save(entry(mod, session_id="c", review="rejected"))
    counts = store.counts()
    assert counts == {"accepted": 1, "proposed": 1}


def test_delete_kinds_except_clears_what_a_later_pass_dropped(store, mod):
    store.save(entry(mod, kind="observation"))
    store.save(entry(mod, kind="tip"))
    store.delete_kinds_except("sess_1", {"observation"})
    assert store.find("sess_1", "tip") is None
    assert store.find("sess_1", "observation") is not None


def test_delete_kinds_except_leaves_other_sessions_alone(store, mod):
    store.save(entry(mod, session_id="sess_1", kind="tip"))
    store.save(entry(mod, session_id="sess_2", kind="tip"))
    store.delete_kinds_except("sess_1", set())
    assert store.find("sess_2", "tip") is not None


# --- the property the whole change exists for --------------------------------


@pytest.mark.skipif(
    not DATABASE_URL, reason="TAPES_DATABASE_URL not set; run `make test-pg`"
)
def test_entries_outlive_the_process(mod):
    """A hosted memory service that forgets on restart is not a memory service.
    A second store object against the same database stands in for a redeploy."""
    first = PostgresStore(DATABASE_URL)
    first.migrate()
    first.clear()
    first.save(entry(mod, title="Written before the restart"))
    first.close()

    second = PostgresStore(DATABASE_URL)
    try:
        survived = second.find("sess_1", "observation")
        assert survived is not None, "the entry did not survive"
        assert survived.title == "Written before the restart"
    finally:
        second.clear()
        second.close()


def test_open_store_falls_back_to_memory_without_a_database_url():
    """Same fallback the hello-world example uses: no credential means the
    cassette still runs, it just does not remember."""
    store = open_store("")
    assert isinstance(store, MemoryStore)
    assert store.durable is False
