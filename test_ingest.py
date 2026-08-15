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


# --- titles ------------------------------------------------------------------

# The reflection that made the queue unreadable, kept verbatim: at 141
# characters it is well past any title length, and its 80th character lands
# mid-word inside "unfixed".
REAL_REFLECTION = (
    "Audited pokemon-kafka milestones and found gym-2 readiness blocked by an "
    "unfixed Viridian Forest blackout — no runs launched, purely a review."
)


def observation_for(session_id, payload):
    client.post(INGEST, json=payload)
    return next(
        e
        for e in entries()
        if e["kind"] == "observation" and session_id in e["sessionIds"]
    )


def test_the_clients_title_is_the_title():
    """The client wrote the reflection, so the client writes its headline. The
    cassette runs no inference and must not paraphrase what it was handed."""
    observation = observation_for(
        "sess_1",
        {
            "sessionId": "sess_1",
            "reflection": REAL_REFLECTION,
            "reflectionTitle": "Gym-2 blocked on the Viridian Forest blackout",
        },
    )
    assert observation["title"] == "Gym-2 blocked on the Viridian Forest blackout"
    assert observation["attrs"]["titleSource"] == "client"


def test_an_untitled_reflection_no_longer_gets_a_mid_word_slice():
    """The regression. Titling an observation `reflection[:80]` cut wherever
    the 80th character landed — here, inside "unfixed" — so the queue read as
    broken rather than terse.

    The fallback is still a prefix of the body, because a service with no
    inference has nothing better to derive from. That is the point of marking
    it `derived`: the cassette cannot write a title, only a client can, and an
    entry that never got one is findable rather than blended in.
    """
    observation = observation_for(
        "sess_1", {"sessionId": "sess_1", "reflection": REAL_REFLECTION}
    )
    title = observation["title"]

    assert title != REAL_REFLECTION[:80]
    assert not title.endswith("unfixed"), "the old cut landed mid-word here"
    assert len(title) <= main.TITLE_MAX
    assert observation["attrs"]["titleSource"] == "derived"


def test_derived_titles_are_filterable():
    """A reviewer sorting out the weak titles, and the title eval sampling only
    real ones, both need to tell the two apart without diffing against bodies."""
    client.post(
        INGEST,
        json={
            "sessionId": "sess_1",
            "reflection": REAL_REFLECTION,
            "reflectionTitle": "Gym-2 blocked on the Viridian Forest blackout",
        },
    )
    client.post(INGEST, json={"sessionId": "sess_2", "reflection": REAL_REFLECTION})

    sources = {
        e["sessionIds"][0]: e["attrs"]["titleSource"]
        for e in entries()
        if e["kind"] == "observation"
    }
    assert sources == {"sess_1": "client", "sess_2": "derived"}


def test_a_derived_title_ends_on_a_word_boundary():
    """Whatever the fallback produces, it may not end mid-word: that is the
    tell that made the old titles read as broken rather than terse."""
    long_prose = "Provisioned " + "a very long clause about Confluent " * 6 + "today."
    observation = observation_for(
        "sess_1", {"sessionId": "sess_1", "reflection": long_prose}
    )
    title = observation["title"]
    assert len(title) <= main.TITLE_MAX
    # every word in the title survives intact in the source prose
    assert title.split()[-1] in long_prose.split()


def test_a_blank_client_title_falls_back_rather_than_titling_nothing():
    observation = observation_for(
        "sess_1",
        {
            "sessionId": "sess_1",
            "reflection": REAL_REFLECTION,
            "reflectionTitle": "   ",
        },
    )
    assert observation["title"]
    assert observation["attrs"]["titleSource"] == "derived"


def test_an_overlong_tip_title_is_cut_on_a_word_boundary_too():
    """Tips arrive titled, but nothing stops a model returning a sentence."""
    client.post(
        INGEST,
        json={
            "sessionId": "sess_1",
            "reflection": "Prose.",
            "tip": {
                "id": "r",
                "title": "Trace the turn-385 blackout before attempting any further "
                "gym-2 work because the party cannot survive it at level 8",
                "body": "B",
            },
        },
    )
    tip = next(e for e in entries() if e["kind"] == "tip")
    assert len(tip["title"]) <= main.TITLE_MAX
    assert tip["title"].split()[-1] in tip["title"]
    assert not tip["title"].endswith(",")


def test_clean_title_collapses_whitespace():
    assert main.clean_title("  Gym-2   blocked\non the\tblackout ") == (
        "Gym-2 blocked on the blackout"
    )


def test_derive_title_stops_at_the_first_sentence():
    assert main.derive_title("Gym-2 is blocked. The blackout at turn 385 is why.") == (
        "Gym-2 is blocked."
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
