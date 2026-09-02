# memory cassette

A `cassette/v1alpha1` service that holds derived agent memory, written in Python
because a cassette is an independently deployed HTTP service rather than an
in-process plugin. That is what lets the eventual Cognee build call the Cognee
SDK directly instead of proxying to it.

Status: running on a live Cognee engine, embedded in the process rather than
proxied to. The routes are written against a pre-existing `/v1/memory` client
contract, and swapping the store did not change one of them.

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
         |                    + cognify       POST /recall
         |                       |            MCP  memory.recall
         |                     Reject
         |                       v
         +------- Reject ---> rejected
                              (hidden, never recalled)

     any state --- DELETE /entries/{id} ---> gone


  Each review state is a Cognee dataset, and only the accepted one is ever
  cognified:

        memory_proposed     memory_rejected        memory_accepted
        (stored, no graph)  (stored, no graph)     (stored + KNOWLEDGE GRAPH)
                                                          |
                                                          v
                                                    recall() reads
                                                    here and nowhere else
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

With Cognee underneath, that second property stopped being a filter and became
structural. Only the accepted dataset is ever cognified, so unreviewed prose is
not in the knowledge graph for recall to surface by accident. A filter can be
forgotten in a later refactor; an empty graph cannot.

## Run it

```bash
export LLM_API_KEY=sk-...   # Cognee builds the graph with an LLM
make up                     # postgres + tapes + this cassette
curl localhost:8082/v1/cassettes
```

Three services: Postgres, a released `tapes serve api` on 8082, and this
cassette on 9998, with Cognee running inside it. Tapes fetches
`http://memory:9998/openapi` every 10s and republishes every path under
`/v1/cassettes/memory/`.

Cognee is embedded rather than run as a fourth service. A cassette is an
independently deployed HTTP service precisely so it can call an SDK directly
instead of proxying to one, so there is no second network hop and no second
thing to secure. `LLM_API_KEY` is the only vendor credential: embeddings run
locally through fastembed, so switching the LLM to Anthropic or anything else
does not drag in a second provider.

```bash
LLM_PROVIDER=anthropic LLM_MODEL=claude-sonnet-4-5 LLM_API_KEY=sk-ant-... make up
```

Core is pinned to a published image (`tapes:v0.34.0`, the release that added MCP
cassettes) rather than built from source. A cassette never depends on the tapes
tree, and pinning the tag is what makes "which contract does this run against"
a question with an answer.

```bash
make test                  # the suite on the volatile store, no venv to manage
make test-cognee           # the same suite against a real Cognee engine
make down                  # stop        (ARGS=-v also drops the db volume)
make logs                  # follow this cassette
make help
```

`make test-cognee` needs no API key and costs nothing. Ingesting, revising, and
rejecting call no LLM — only accepting does — so the whole store contract runs
against a real engine on its file-based defaults in a throwaway directory.

Requires [uv](https://docs.astral.sh/uv/) and Docker.

## Where memories are kept

Cognee, which is the store rather than something behind it. The mapping is
deliberate, because Cognee has its own model of the same problem:

| the cassette | Cognee |
| --- | --- |
| a review state | a dataset (`memory_proposed` / `_accepted` / `_rejected`) |
| an entry | a data item in that dataset |
| the entry's contract fields | `external_metadata` on that item |
| accepting | `cognify()` — the graph is built here, and only here |
| recall | `recall()` over the accepted dataset alone |

Two things follow that are worth knowing before reading `cognee_store.py`.

**Identity is the cassette's, not Cognee's.** Cognee mints a fresh `data_id` per
row and dedupes rows by content hash, so a revision or a review decision lands
on a new row. The contract's `id` therefore lives inside `external_metadata` and
outlives every row that carries it, which is what keeps a link to an entry valid
while its text is revised and its review state moves.

**A revision is forget-then-add, not Cognee's `update()`.** `update()`
re-cognifies, and a proposed entry must not cost an LLM call before anyone has
agreed to keep it.

Postgres is still in the stack, but the cassette no longer writes rows into it:
`provision.sql` creates a database Cognee owns (its relational tables, and its
embeddings via pgvector), and the graph itself lives on Kuzu under
`COGNEE_STORAGE_DIR`. `cassette.toml` now declares `tables = []`, because this
cassette takes no grant on the tapes database at all.

Without `COGNEE_ENABLED` it falls back to an in-process store, so you can try it
with no engine and no credential. That store is volatile, and `/ping` says which
one answered, because a memory service that forgets is worth noticing early:

```json
{"status":"ok","cassette":"memory","store":"cognee","durable":true,"indexing":false}
```

Treat `"durable": false` as fine for a look around and wrong for anything
hosted. `"indexing": true` means an accepted entry's graph is still building:
accepting returns before cognify finishes, so there is a window where an entry
is accepted but not yet recallable. Reported, that window is a state; unsaid, it
reads as recall having lost something.

## Running it on AWS

**The container is no longer stateless**, and that is the biggest deployment
consequence of moving to Cognee. The knowledge graph lives on Kuzu on disk, so
the `cognee-data` volume *is* the memory: a host with ephemeral storage loses
every accepted entry's graph on redeploy, while the entries themselves survive
in Postgres. Persistent storage is now a requirement rather than a convenience.

Two region facts for **us-west-1**, where `deploy/aws.sh` lands:

- **App Runner is not available there.** Nearest is us-west-2.
- **Lightsail Containers and ECS Fargate are.** Lightsail nodes have only
  ephemeral storage, which disqualifies them again now that the graph is kept on
  disk — this had stopped mattering when the container was stateless.

What it needs, wherever it runs:

| | |
| --- | --- |
| `LLM_API_KEY` | the provider Cognee builds the graph with. No default, and nothing works without it |
| `COGNEE_ENABLED` | `true` for the real store; unset runs the volatile fallback |
| `COGNEE_STORAGE_DIR` | a **persistent volume**. The graph lives here |
| `DB_*`, `VECTOR_DB_PROVIDER` | Postgres for Cognee's own tables and embeddings (`provision.sql`) |
| `CASSETTE_NAME` | defaults to `memory`; drives route, database, and role names |
| port | 9998 |
| reachability | the tapes core that registers it must be able to fetch `/openapi`, and outbound HTTPS to the LLM provider |

Core fetches that document with **no redirects followed**, and the API and the
document must share an origin. So nothing that bounces through a login can sit
in front of it: no auth-redirecting ALB, and no serving the document from a CDN
while the API lives elsewhere.

**On locking it down.** The cassette has no authentication of its own. Anything
that can reach it can read and write your memory — and, now that accepting an
entry calls an LLM, spend your credential — so the access boundary has to come
from the network. A single EC2 instance running this compose file, with a
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

`push.sh` recreates only the `memory` service; Postgres holds Cognee's tables
and tapes fronts the cassette, so neither restarts. The graph rides through on
the `cognee-data` volume, which is why recreating that one container is safe.
It also refreshes the box's ECR login before pulling: those tokens last 12
hours, and once one goes stale the pull fails with "repository does not exist or
may require 'docker login'", which reads like a missing image rather than an
expired credential.

**On the box you already have.** `aws.sh` bakes compose and `.env` in through
cloud-init, which does not re-run, so an existing instance needs the new
`compose.yaml`, the `LLM_API_KEY` line in `.env`, and the new `provision.sql`
applied by hand over SSM before `push.sh` will do anything useful. The instance
type also wants to go from `t4g.small` to `t4g.medium`: 2 GB is where an
embedded graph database and a local embedding model start getting killed by the
OOM reaper mid-cognify.

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
| `POST /entries/{id}/review` | Accept or reject, in either direction |
| `DELETE /entries/{id}` | Remove an entry outright, in any review state |
| `POST /recall` | Search accepted memory. Published over MCP as `memory.recall` |

A dream payload maps onto the kind enum with nothing left over: a reflection
becomes an `observation`, a tip becomes a `tip`. Both land `proposed` and stay
out of recall until a human accepts them.

An acceptance is not final. Reviewing an accepted entry as `rejected` moves
it out of recall and keeps it on the record; `DELETE` removes it entirely.
Both matter because clients push a cheap template reflection first and an
LLM-written one seconds later: if the second never lands, the template is
what sits in the queue, and one of those accepted by mistake needs a way out.

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

# Accepting is what builds the graph, and it returns before that finishes.
# Wait for indexing to clear, then recall.
curl -s localhost:9998/ping        # {"indexing":true} while cognify runs
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

- **Nothing automated covers the LLM path.** `make test-cognee` verifies the
  store contract against a real engine but deliberately makes no LLM calls, so
  cognify and graph-backed recall are exercised only by hand. They do work: a
  query sharing no words with an entry's title (`"what did the expensive model
  do?"`) comes back from the graph. But `recall()` falls back to substring
  matching when the graph answers nothing, so a broken graph degrades quietly
  rather than erroring — worth knowing when judging result quality.
- **Every write reads the whole dataset.** `find`, `get`, and `save` each list
  every row to match on `external_metadata`, because that is where the contract's
  identity lives. Fine at a review queue's scale, and the first thing to fix if
  this ever holds more than a few thousand entries.
- **`depends.views` is empty**, so this reads none of tapes' own data. Adding
  `["sessions", "spans"]` takes SELECT grants on `tapes_v1.<view>`, which means
  extending `provision.sql`. `raw_turns` may never be listed: core refuses the
  manifest outright.
- **No authentication.** See the note above; the boundary is the network.
- **No CI.** `deploy/push.sh` builds and publishes the image, but a human runs
  it; nothing builds on merge, and no test gates a deploy.
- **Older clients may still call `/v1/memory/*`**, not `/v1/cassettes/memory/*`.
