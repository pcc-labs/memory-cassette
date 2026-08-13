"""Ingest behavior, especially what happens when one session is reflected twice.

Run: make test   (or: uv run --with fastapi --with httpx --with pytest pytest)
"""

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
INGEST = f"{main.PREFIX}/ingest/dream"
ENTRIES = f"{main.PREFIX}/entries"


@pytest.fixture(autouse=True)
def clean_store():
    main.store.clear()
    yield
    main.store.clear()


def dream(session_id, reflection, tip_title, tip_body, tip_id="r"):
    return {
        "sessionId": session_id,
        "observations": ["some fact"],
        "reflection": reflection,
        "tip": {"id": tip_id, "title": tip_title, "body": tip_body},
    }


def entries(review="proposed"):
    return client.get(ENTRIES, params={"review": review}).json()["items"]


# --- the duplicate bug -------------------------------------------------------


def test_reflecting_one_session_twice_keeps_one_entry_per_kind():
    """A client may push twice per session: a deterministic template pass, then
    an LLM upgrade ~15s later. Both describe the same session, so the second
    must supersede the first rather than pile up in the review queue."""
    client.post(
        INGEST, json=dream("sess_1", "Template prose.", "Template tip", "Template body")
    )
    client.post(INGEST, json=dream("sess_1", "LLM prose.", "LLM tip", "LLM body"))

    items = entries()
    assert len(items) == 2, f"expected one observation + one tip, got {len(items)}"
    assert sorted(e["kind"] for e in items) == ["observation", "tip"]


def test_the_later_reflection_wins():
    client.post(
        INGEST, json=dream("sess_1", "Template prose.", "Template tip", "Template body")
    )
    client.post(INGEST, json=dream("sess_1", "LLM prose.", "LLM tip", "LLM body"))

    observation = next(e for e in entries() if e["kind"] == "observation")
    tip = next(e for e in entries() if e["kind"] == "tip")
    assert observation["body"] == "LLM prose."
    assert tip["title"] == "LLM tip"


def test_identity_is_stable_across_a_re_reflection():
    """A client links to an entry by id, so superseding must not mint a new
    one. firstSeenAt is when we first learned this, and does not move."""
    client.post(INGEST, json=dream("sess_1", "First.", "T", "B"))
    before = next(e for e in entries() if e["kind"] == "observation")

    client.post(INGEST, json=dream("sess_1", "Second.", "T", "B"))
    after = next(e for e in entries() if e["kind"] == "observation")

    assert after["id"] == before["id"]
    assert after["firstSeenAt"] == before["firstSeenAt"]
    assert after["lastSeenAt"] >= before["lastSeenAt"]


def test_different_sessions_stay_separate():
    client.post(INGEST, json=dream("sess_1", "One.", "T1", "B1"))
    client.post(INGEST, json=dream("sess_2", "Two.", "T2", "B2"))
    assert len(entries()) == 4


# --- the review gate under supersession --------------------------------------


def test_superseding_an_accepted_entry_sends_it_back_for_review():
    """The gate's promise is that only text a human accepted is recallable.
    New prose under an old acceptance would break that silently."""
    client.post(INGEST, json=dream("sess_1", "Original prose.", "T", "B"))
    observation = next(e for e in entries() if e["kind"] == "observation")
    client.post(f"{ENTRIES}/{observation['id']}/review", json={"review": "accepted"})
    assert len(entries("accepted")) == 1

    client.post(INGEST, json=dream("sess_1", "Rewritten prose.", "T", "B"))

    assert entries("accepted") == []
    reproposed = next(e for e in entries() if e["kind"] == "observation")
    assert reproposed["body"] == "Rewritten prose."


def test_an_identical_re_push_leaves_an_acceptance_alone():
    """Re-opening a session page re-fires the reflection. Identical content is
    not new information, so it must not churn the queue."""
    client.post(INGEST, json=dream("sess_1", "Same prose.", "T", "B"))
    observation = next(e for e in entries() if e["kind"] == "observation")
    client.post(f"{ENTRIES}/{observation['id']}/review", json={"review": "accepted"})

    client.post(INGEST, json=dream("sess_1", "Same prose.", "T", "B"))

    assert len(entries("accepted")) == 1
    assert (
        next(e for e in entries("accepted") if e["kind"] == "observation")["body"]
        == "Same prose."
    )


def test_a_reflection_with_no_tip_does_not_strand_the_previous_one():
    """The LLM pass can come back without a tip. The earlier tip was derived
    from a reflection that no longer stands, so it must not outlive it."""
    client.post(INGEST, json=dream("sess_1", "Prose.", "Stale tip", "Stale body"))
    assert any(e["kind"] == "tip" for e in entries())

    client.post(
        INGEST,
        json={"sessionId": "sess_1", "observations": [], "reflection": "Prose only."},
    )

    assert not any(e["kind"] == "tip" for e in entries())
