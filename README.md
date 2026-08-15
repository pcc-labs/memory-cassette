# memory cassette

A `cassette/v1alpha1` service that holds derived agent memory, written in Python
because a cassette is an independently deployed HTTP service rather than an
in-process plugin. That is what lets the eventual Cognee build call the Cognee
SDK directly instead of proxying to it.

Status: admission spike. The store is in memory and Cognee is not wired up yet.
The routes are written against a pre-existing `/v1/memory` client contract so
swapping the store does not change them.

## How memories work

```
  1  CAPTURE                  2  REFLECT                       3  STORE
  ---------                   ----------                       --------
  an agent run,               a client reflects on the         this cassette
  captured by tapes           session, often more than once

   [ session ] ----------->   t+0s   template pass  (free)
                              t+15s  LLM upgrade    (better)
                                          |
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

**A session's memory is revised, not accumulated.** A client may reflect the
same session many times over. Those are revisions of one judgment, so they
collapse onto one entry per `(sessionId, kind)`. The entry keeps its `id` and
`firstSeenAt` across a revision, so a link to it stays valid.

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

Two region facts for **us-west-1**, where `deploy/aws.sh` lands:

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

`deploy/aws.sh` is that path, scripted: one EC2 box, no inbound SSH (shell
access goes through SSM), all three services under compose, and a security
group that admits exactly one source address. `deploy/tunnel.sh` reaches the
box from any network over an SSM port forward, for when that source address
goes stale. Both scripts are the AWS path specifically; on any other host, the
table above is the whole contract.

`deploy/push.sh` is how new code gets there:

```sh
./deploy/push.sh                 # build linux/arm64, push to ECR, roll the box
./deploy/push.sh --no-restart    # build and push only
```

Use it rather than re-running `aws.sh` for a code change. `aws.sh` exits early
once the box is up and never touches the image — but only after converging the
security group, which revokes every allowlisted address that is not your
current one. A redeploy has no business changing who can reach the box.

`push.sh` recreates only the `memory` service; Postgres holds the entries and
tapes fronts the cassette, so neither restarts. It also refreshes the box's ECR
login before pulling: those tokens last 12 hours, and once one goes stale the
pull fails with "repository does not exist or may require 'docker login'",
which reads like a missing image rather than an expired credential.

**On transport encryption.** The allowlist controls who can connect, not who
can observe in transit: the deployed box serves plain HTTP, so request and
response bodies cross the network in the clear. This repo deliberately ships
no TLS story, because the right one depends on where you host it. If that
matters for your deployment, either give the box a DNS name and put an
auto-certifying proxy such as Caddy in front of it, or remove the public port
entirely with Tailscale or another WireGuard mesh. The trade-offs are written
up in [issue #1](https://github.com/pcc-labs/memory-cassette/issues/1).

## What it does

| Route | Purpose |
| --- | --- |
| `POST /ingest/dream` | Take a client's dream output verbatim |
| `GET /entries` | List by review state, kind, status, or substring |
| `GET /entries/{id}` | Read one |
| `POST /entries/{id}/review` | Accept or reject |
| `POST /recall` | Search accepted memory. Published over MCP as `memory.recall` |

A dream payload maps onto the kind enum with nothing left over: a reflection
becomes an `observation`, a tip becomes a `tip`. Both land `proposed` and stay
out of recall until a human accepts them.

## Wiring a client to it

Any client that produces reflections can post here: point its memory base at
`http://localhost:8082` (this stack) or at the deployed address, and send each
finished reflection to `POST /ingest/dream`. Revisions of the same session
should reuse the `sessionId`, so they replace the earlier entry rather than
pile up in the review queue. The payload shape is in the end-to-end check
below; a client's own repo carries its wiring specifics.

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
  were written against the client contract so the backend can be swapped
  without touching them.
- **`depends.views` is empty**, so this reads none of tapes' own data. Adding
  `["sessions", "spans"]` takes SELECT grants on `tapes_v1.<view>`, which means
  extending `provision.sql`. `raw_turns` may never be listed: core refuses the
  manifest outright.
- **No authentication.** See the note above; the boundary is the network.
- **No CI.** `deploy/push.sh` builds and publishes the image, but a human runs
  it; nothing builds on merge, and no test gates a deploy.
- **Older clients may still call `/v1/memory/*`**, not `/v1/cassettes/memory/*`.
