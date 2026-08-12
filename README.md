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

## Where memories are kept

Postgres, in a schema this cassette owns and migrates itself (`store.py`). Core
creates nothing and never holds the credential: `provision.sql` makes the role
and grant, `cassette.toml` declares the table, and the cassette builds it at
startup.

Without `TAPES_DATABASE_URL` it falls back to an in-process store, so you can
try it with no database. That store is volatile, and `/ping` says which one
answered, because a memory service that forgets is worth noticing early:

```json
{"status":"ok","cassette":"memory","store":"postgres","durable":true}
```

Treat `"durable": false` as fine for a look around and wrong for anything
hosted.

## Running it on AWS

The container is stateless, since all state lives in Postgres. That makes it
ordinary to host: any container platform plus a managed database works, and a
redeploy loses nothing.

Two region facts for **us-west-1**, which is where this is headed:

- **App Runner is not available there.** Nearest is us-west-2.
- **Lightsail Containers and ECS Fargate are.** Lightsail nodes have only
  ephemeral storage, which no longer disqualifies them now that nothing is kept
  on disk.

What it needs, wherever it runs:

| | |
| --- | --- |
| `TAPES_DATABASE_URL` | Postgres DSN. RDS, Lightsail managed database, or a container |
| `CASSETTE_NAME` | defaults to `memory`; drives route, schema, and role names |
| port | 9998 |
| reachability | the tapes core that registers it must be able to fetch `/openapi` |

Core fetches that document with **no redirects followed**, and the API and the
document must share an origin. So nothing that bounces through a login can sit
in front of it: no auth-redirecting ALB, and no serving the document from a CDN
while the API lives elsewhere.

**On locking it down.** The cassette has no authentication of its own. Anything
that can reach it can read and write your memory, so the access boundary has to
come from the network. A single EC2 instance running this compose file, with a
security group restricted to your own address, is the shortest path to that: one
box, real disk, and an allowlist. Lightsail is less work to host but publishes a
public HTTPS endpoint with no IP allowlist, which is the opposite of what a
personal memory store wants.

None of this has been deployed yet. It is what the image needs, not a record of
a running system.

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

- **Cognee.** `[[config]] cognee_base_url` is declared and unused. The routes
  were written against the console's contract so the backend can be swapped
  without touching them.
- **`depends.views` is empty**, so this reads none of tapes' own data. Adding
  `["sessions", "spans"]` takes SELECT grants on `tapes_v1.<view>`, which means
  extending `provision.sql`. `raw_turns` may never be listed: core refuses the
  manifest outright.
- **No authentication.** See the note above; the boundary is the network.
- **No CI.** Nothing builds or publishes an image.
- **The console still calls `/v1/memory/*`**, not `/v1/cassettes/memory/*`.
