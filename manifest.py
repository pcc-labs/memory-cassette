"""The metadata tapes core admits this cassette on.

Two encodings of one schema exist. `cassette.toml` is what a registry or an
orchestrator reads before anything is running. This module is the copy that
rides inside the served OpenAPI document as the `x-tapes-cassette` root
extension, and it is the only one core reads. Keep them in sync.

The name drives three namespaces: the public route (/v1/cassettes/memory), the
Postgres schema ("memory"), and the role ("cassette_memory"). Building the
manifest from the runtime name rather than hardcoding it means the same image
installed under a second name publishes a correct document for that name too.
"""

from __future__ import annotations

VERSION = "0.0.1"

# Contract views this cassette reads. Empty for the admission spike: step 2
# adds ["sessions", "spans"], which become SELECT grants on tapes_v1.<view>.
#
# `raw_turns` may never appear here. Core refuses the whole manifest if it
# does, by design: memory is distilled from the derived read model so it
# inherits re-derivability.
DEPENDS_VIEWS: list[str] = []


def manifest(name: str) -> dict:
    return {
        "kind": "cassette/v1alpha1",
        "cassette": {
            "name": name,
            "version": VERSION,
            "display_name": "Memory",
            "description": (
                "Derived agent memory. Ingests client reflections and "
                "dreams; recall is exposed to agents over MCP."
            ),
            "license": "MIT",
            "homepage": "https://github.com/papercomputeco/tapes",
            "image": f"papercompute/memory-cassette:{VERSION}",
            "port": 9998,
        },
        "depends": {
            "core": "v1",
            "views": DEPENDS_VIEWS,
        },
        "api": {
            "health": "/ping",
            "openapi": "/openapi",
            "prefix_path": "api",
        },
        # Declared, not created. Core publishes this and does nothing about it;
        # the deployment provisions the schema and role (see provision.sql) and
        # the cassette runs its own migration at startup (store.py: DDL).
        "tables": [{"name": "entries"}],
        "config": [
            {
                "key": "cognee_base_url",
                "type": "string",
                "default": "http://cognee:8000",
                "description": (
                    "Base URL of the self-hosted Cognee instance. Must never "
                    "point at Cognee Cloud."
                ),
            },
            {
                "key": "review_required",
                "type": "bool",
                "default": True,
                "description": (
                    "Withhold entries from recall until a human accepts them."
                ),
            },
        ],
    }
