# memory cassette

A `cassette/v1alpha1` service that holds derived agent memory, written in Python
because a cassette is an independently deployed HTTP service rather than an
in-process plugin. That is what lets the eventual Cognee build call the Cognee
SDK directly instead of proxying to it.

Status: admission spike. The store is in memory and Cognee is not wired up yet.
The routes are written against the console's `/v1/memory` contract so swapping
the store does not change them.

## How memories work

```
  1  CAPTURE                  2  REFLECT                       3  STORE
  ---------                   ----------                       --------
  an agent run,               paperplane, on the console       this cassette
  captured by tapes           session page, reflects twice

   [ session ] ----------->   t+0s   template pass  (free)
                 read via     t+15s  LLM upgrade    (better)
                 paperd                   |
                                          |  POST /ingest/dream, once per pass
                                          v
                        +-------------------------------------------+
                        |     upsert on (sessionId, kind)           |
                        |                                           |
                        |  a later pass REPLACES the earlier one,   |
                        |  so revisions never pile up in the queue  |
                        +-------------------------------------------+
                                          |
                          +---------------+---------------+
                          v                               v
                   [ observation ]                     [ tip ]
                   one per session                 one per session


  4  REVIEW GATE                          5  RECALL
  -------------                           ---------

     proposed --- Accept ---> accepted -----> GET  /entries?review=accepted
         |                                    POST /recall
         |                                    MCP  memory.recall
         +------- Reject ---> rejected
                              (hidden, never recalled)
```

Two properties worth stating, because both are load-bearing.

**A session's memory is revised, not accumulated.** Paperplane reflects the same
session on every page load and again on every regenerate. Those are revisions of
one judgment, so they collapse onto one entry per `(sessionId, kind)`. The entry
keeps its `id` and `firstSeenAt` across a revision, so a link to it stays valid.

**Nothing is recallable until a human accepts it.** And if a revision changes an
already-accepted entry's text, it drops back to `proposed`. Otherwise prose
nobody reviewed would inherit an old acceptance, which is exactly what the gate
exists to prevent.

## Run it

```bash
make up                    # postgres + tapes + this cassette
curl localhost:8082/v1/cassettes
```

Three services: Postgres, a released `tapes serve api` on 8082, and this
cassette on 9998. Tapes fetches `http://memory:9998/openapi` every 10s and
republishes every path under `/v1/cassettes/memory/`.

Core is pinned to a published image (`tapes:v0.34.0`, the release that added MCP
cassettes) rather than built from source. A cassette never depends on the tapes
tree, and pinning the tag is what makes "which contract does this run against"
a question with an answer.

```bash
make test                  # 7 tests, no venv to manage
make down                  # stop        (ARGS=-v also drops the db volume)
make logs                  # follow this cassette
make help
```

Requires [uv](https://docs.astral.sh/uv/) and Docker.

## What it does

| Route | Purpose |
| --- | --- |
| `POST /ingest/dream` | Take paperplane's `dreamOnSession` output verbatim |
| `GET /entries` | List by review state, kind, status, or substring |
| `GET /entries/{id}` | Read one |
| `POST /entries/{id}/review` | Accept or reject |
| `POST /recall` | Search accepted memory. Published over MCP as `memory.recall` |

Paperplane's dream output maps onto the console's existing kind enum with
nothing left over: a reflection becomes an `observation`, a tip becomes a `tip`.
Both land `proposed` and stay out of recall until a human accepts them.

## Wiring paperplane to it

`pcc-labs/paperplane` posts here. Set **Memory base** in its settings to
`http://localhost:8082` (this stack) and every reflection it finishes lands in
the proposed queue. Blank disables the push, which is the default.

The hook wraps `putCachedReflection` in `src/content/console-reflect.js`, so all
three reflection kinds are covered: the deterministic template, the LLM upgrade,
and the stored server-side one. `src/api.js:toDreamPayload` flattens them into
one payload, and the request goes through the service worker because a content
script on the cloud console cannot reach loopback.

## End-to-end check

```bash
B=localhost:8082/v1/cassettes/memory

curl -s -X POST $B/ingest/dream -H 'content-type: application/json' -d '{
  "sessionId": "sess_01H8XK",
  "observations": ["47 turns over 3h", "$4.12, 3.4x your median"],
  "reflection": "A long opus session that never switched down.",
  "tip": {"id": "model-overrun", "title": "Switch down after design",
          "body": "40 turns of mechanical edits stayed on opus."}
}'

# Recall is empty until a human accepts.
curl -s -X POST $B/recall -H 'content-type: application/json' -d '{"query":"opus"}'
curl -s -X POST $B/entries/<id>/review -H 'content-type: application/json' -d '{"review":"accepted"}'
curl -s -X POST $B/recall -H 'content-type: application/json' -d '{"query":"opus"}'
```

As an agent would reach it:

```bash
curl -s -X POST localhost:8082/v1/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"memory.recall","arguments":{"query":"opus"}}}'
```

## Two things that will bite

**The document must be OpenAPI 3.1.** Core gates the MCP bridge on it
(`api/cassetterunner/mcp.go`): `x-tapes-mcp` on a 3.0 document is refused, and
it takes the whole document with it. FastAPI emits 3.1 natively, so do not
override the version. The `hello-world` example compiles to 3.0 only because it
declares no MCP tools.

**The anchors must not be operations.** `/ping` and `/openapi` carry
`include_in_schema=False`. Every remaining path has to sit below `/api/memory`
or core refuses the document whole.

## Not done yet

- Cognee. `[[config]] cognee_base_url` is declared and unused.
- `depends.views`. Empty, so this reads none of tapes' data. Adding
  `["sessions", "spans"]` takes SELECT grants on `tapes_v1.<view>` and needs a
  `provision.sql`. `raw_turns` may never be listed.
- The console still calls `/v1/memory/*`, not `/v1/cassettes/memory/*`.
