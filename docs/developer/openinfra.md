# OpenInfra

OpenInfra publishes the live state of the GPU hosts and inference engines a
deployment runs itself, on a public console page, `/openinfra`. It shows each
GPU's utilization, memory, power, temperature and interconnect; each host's CPU
and memory; and each SGLang or vLLM engine's queue, KV cache, prefix-cache hits,
token throughput and latencies, sampled every second. History runs from
fifteen minutes to seven days. The page also shows requests and tokens served in
the last hour and day, and the shape and latency of recent requests, without
prompts, completions, addresses or request ids.

It is off unless a deployment turns it on, in two separate steps: collecting
and publishing.

## How the data gets there

A small reporter runs on each GPU host, apart from the gateway. It reads the
GPUs, the host and the engines locally, and every second posts one JSON
snapshot to the gateway over HTTPS. The gateway stores the snapshots in its own
PostgreSQL database, and the page reads them back.

```text
GPU host: reporter ──POST /internal/openinfra/ingest──▶ edge ──▶ console ──▶ gateway ──▶ PostgreSQL
            Authorization: Bearer <OPENINFRA_INGEST_TOKEN>

browser ──/openinfra──▶ console page ──GET /public-openinfra, /public-openinfra/history──▶ gateway
```

- **Only outbound HTTPS from the GPU hosts.** The gateway never connects to a
  host, so nothing on it needs to be reachable.
- **Any address that reaches the gateway works.** It can be the public site,
  or the backend's address on a private network. The gateway serves the ingest
  route itself; the console only relays it (see
  [The Public Path Table](public-path-table.md)).
- **Any gateway replica can take a snapshot.** Replicas share the database,
  and each scheduled job (rollups and retention, archives) runs on one replica
  at a time.
- **The routing engine does not read these metrics.** They feed the page only.

## Turning it on

1. **Collecting.** Set `OPENINFRA_INGEST_TOKEN` (Admin, Settings, Site & Setup,
   Advanced settings, Integrations) to a long random string, for example the
   output of `openssl rand -hex 32`. While it is empty,
   `POST /internal/openinfra/ingest` answers `404` and nothing is collected.
2. **Publishing.** Add `"openinfra": true` to `features` in the
   `SITE_CONFIGURATION` document. The console then serves `/openinfra` and links
   it from the header and footer, and `GET /public-openinfra` and its history
   answer. Without the flag both answer `404`, even while reporters post, so a
   deployment can collect before it publishes.
3. **Optional settings**, all Integrations settings and all applied within
   seconds:
   - `OPENINFRA_ENGINE_ORDER`: engine names, comma-separated, in the order the
     page shows their tabs. Engines not listed follow, the one that served the
     most tokens in the last day first.
   - `OPENINFRA_RETIRED_ENGINES`: engine ids, comma-separated, left out of the
     page, the served totals, the history and exports. What they reported stays
     stored, so removing an id brings it back. Retire an engine by its id
     rather than relying on one engine replacing another: one endpoint may serve
     several models, and the same localhost endpoint repeats across hosts.
   - `OPENINFRA_RETENTION_DAYS`: how many days the per-second samples and
     request events stay in the database once their week is archived; 7 by
     default. `OPENINFRA_DELETE_UNARCHIVED` deletes them past it even without
     an archive. See
     [Retention and weekly archives](#retention-and-weekly-archives).

### Pointing another deployment at this page

A fleet should report to one gateway. A second deployment of the same fleet,
such as a staging site, can link to that gateway's page instead of collecting
its own copy: set `OPENINFRA_PUBLIC_URL` in the second deployment's console
(frontend container) environment to the page's public HTTPS address, for
example `https://gateway.example.org/openinfra`. The header and footer then link
there, opening it in a new tab, and that deployment's `/openinfra` redirects
there. Its own `features.openinfra` flag and ingest token are not needed and
can stay unset; its `/public-openinfra` endpoints keep answering `404`. The
value is read when the console starts, and recreating the console applies a
change. A URL that is not HTTPS, or that carries credentials, is ignored with a
warning.

## Running a reporter on a GPU host

The reporter is `ops/openinfra/reporter.py`. It uses only the Python standard
library, so the host's system Python runs it, and it needs `nvidia-smi`. Copy
`ops/openinfra/` to the host and write a node configuration, starting from
`ops/openinfra/node.example.json`:

| Field | Meaning |
|---|---|
| `node_id` | The host's id. GPU ids are `<node_id>:<index>`, so each host needs its own |
| `hostname`, `region`, `cluster` | Shown on the page |
| `flush_interval` | Seconds between snapshots; 1 by default. The gateway keeps one sample a second, so a shorter interval adds nothing |
| `log_interval` | Seconds between the flushes the reporter logs; 60 by default, 0 logs every one |
| `sglang` | The engines on the host, SGLang and vLLM alike. An empty list says the host has none |
| `sglang[].url` | Where to scrape the engine: SGLang's `/v1/loads` (preferred) or `/metrics`, vLLM's `/metrics` |
| `sglang[].id` | The engine's id, unique across the fleet |
| `sglang[].name` | Its tab on the page |
| `sglang[].engine` | `sglang` or `vllm` |
| `sglang[].endpoint` | The address shown for it |
| `sglang[].gpu_indexes` | The GPUs it runs on; its tab shows only those |

Try one snapshot before installing the service:

```bash
OPENINFRA_URL=https://gateway.example.org OPENINFRA_TOKEN=... \
  python3 reporter.py --config node.json --once
```

`ops/openinfra/openinfra-reporter.service` runs it under systemd, reading
`OPENINFRA_URL`, `OPENINFRA_TOKEN` and `OPENINFRA_NODE_CONFIG` from
`/etc/openinfra/reporter.env` (template: `ops/openinfra/reporter.env.example`).
The unit's header lists the install commands. Keep the env file readable by root
and the reporter's group only: it holds the token.

Engine details:

- **SGLang.** Start it with `--enable-metrics`. The reporter reads `/v1/loads`
  and the sibling `/metrics` for the request and token counters that give
  requests per minute, time to first token and time per output token on every
  flush. Without them, those come from the engine's container logs, read once a
  minute and held in between, which needs `docker` (the docker group, or
  `sudo -n docker`). An engine whose `/v1/loads` needs its API key, which the
  reporter does not send, is scraped through `/metrics`.
- **Recent requests.** Request events come only from the engines' container
  logs, whatever counters an engine has, so the reporter's account needs
  `docker` for them (the unit's install commands add `openinfra` to the docker
  group). Without it everything else is still reported and the page's recent
  requests stay empty; the reporter says why once in its log
  (`journalctl -u openinfra-reporter | grep 'engine logs'`).
- **vLLM.** It has no `/v1/loads`: scrape `/metrics` and set
  `"engine": "vllm"`.
- **DCGM (optional).** Tensor-core, DRAM, NVLink and PCIe charts need NVIDIA's
  dcgm-exporter. Install it with the field list and override in
  `ops/openinfra/dcgm/`; the reporter reads `http://127.0.0.1:9400/metrics`
  when it answers, and reports everything else without it. A host without
  DCGM gets its PCIe throughput from an `nvidia-smi dmon` the reporter keeps
  running beside it.

The reporter posts once per flush, all engines and GPUs together. It takes each
snapshot in the middle of its second of wall-clock time and sends the time it
took it, so network delay cannot put two snapshots in the same second; a
snapshot that takes longer than the interval skips the next one rather than
queueing it. When the gateway cannot be reached it sends the next snapshot on
schedule; the snapshots in between are not kept. A failure that repeats is
logged once per `log_interval`, with a count of the ones held back. A
User-Agent some edges let through is sent; `OPENINFRA_USER_AGENT` overrides it.

Command-line flags can stand in for a configuration file: `--dashboard` (the
gateway address), `--token`, `--node-id`, `--hostname`, `--region`,
`--cluster`, `--flush-interval`, `--log-interval` and a repeated
`--sglang-url`.

## The ingest API

`POST /internal/openinfra/ingest` takes one host's snapshot, with
`Authorization: Bearer <token>` (or `x-ingest-token: <token>`) carrying
`OPENINFRA_INGEST_TOKEN`:

```json
{
  "ts": 1767225600000,
  "node": {
    "id": "node-a",
    "hostname": "node-a",
    "region": "lab",
    "cpu": {"cpu_util_pct": 12.5, "load1": 0.8, "mem_used_mb": 20480, "mem_total_mb": 515000}
  },
  "gpus": [
    {"index": 0, "name": "Example GPU", "utilization_pct": 91.2,
     "memory_used_mb": 72010, "memory_total_mb": 81559, "temperature_c": 68, "power_w": 612}
  ],
  "sglang": [
    {
      "server": {"id": "node-a-chat", "node_id": "node-a", "name": "chat-model",
                 "engine": "sglang", "gpu_indexes": "0,1"},
      "metrics": {"num_running_reqs": 148, "num_queue_reqs": 12, "gen_throughput": 4120.4,
                  "token_usage": 0.86},
      "requests": [{"ts": 1767225600000, "input_len": 812, "output_len": 230, "ttft_s": 0.41}]
    }
  ]
}
```

- `ts` is when the reporter took the snapshot, in milliseconds by its own
  clock. While that is within a minute of the gateway's clock the snapshot's
  samples are stored at it, and otherwise at the gateway's time. It may be left
  out. Whether a host is online is always the gateway's clock alone.
- `sglang` holds every engine on the host, vLLM included (`engine` says which),
  and may also be a single object. An engine sends parsed `metrics`, its raw
  `prometheus` text, or both; text that exposes several models gives each model
  its own engine row, `<id>:<model>`, and lists the server's request events
  under each.
- Parsing is lenient, so reporters of any age keep working: a number may arrive
  as a numeric string, unknown fields are ignored, and a value of the wrong type
  is dropped. A body of the wrong shape is refused whole.
- A body carries at most 64 GPUs and 16 engines, keeps 20 request events per
  engine, and is limited to 2 MiB. A request event more than 15 minutes older,
  or more than a minute newer, than the snapshot is dropped, both by the
  reporter's clock, and counted in the answer's `requests_dropped`. While the
  reporter's clock is off, its request events move with its samples onto the
  gateway's time.

| Answer | When |
|---|---|
| `200 {"ok": true, "gpus": N, "sglang": M}` | Committed, with `"requests_dropped": K` when request events were dropped |
| `400` | The body is not JSON, or not a snapshot; the message says why |
| `401` | The token is missing or wrong. Checked before the body is read |
| `404` | No token is configured: this deployment does not collect |
| `408`, `413` | The body was too slow or too large |
| `503` | No database, or the write failed; the next snapshot comes as usual |

## What the page reads

Both reads are unauthenticated, serve only what the page shows, and answer
`404` unless the deployment publishes OpenInfra.

- `GET /public-openinfra`: the fleet's state (`healthy`, `busy`, `degraded` or
  `offline`), fleet-wide figures, every host, GPU and engine (under `sglang`,
  in tab order), the requests and tokens served in the last hour and day, and
  each engine's 40 most recent requests.
- `GET /public-openinfra/history?hours=2&node=`: time series for every host, or
  one, over the last `hours`, rounded up to one of 0.25, 0.5, 1, 2, 3, 6, 12,
  24, 48, 72 and 168. A `node` that has never reported answers `404`. Each point
  is the mean of the per-second samples in it, and `grain_ms` says how far
  apart the points are:

  | Window | Points |
  |---|---|
  | 15 minutes | 5 seconds apart |
  | 30 minutes | 10 seconds apart |
  | 1 hour | 20 seconds apart |
  | 2 and 3 hours | a minute apart |
  | 6 to 24 hours | 20 minutes apart |
  | 2 to 7 days | an hour apart |

  The page offers 15 minutes, 2 hours, 24 hours and 7 days, and refreshes every
  5 seconds over 15 minutes and every 30 seconds otherwise.

Each answer is computed at most once every 5 seconds (the dashboard) or, for a
history window, once per spacing of its points up to 30 seconds, however many
people ask, and carries a matching `Cache-Control`.

An administrator can download the data: `GET /admin/openinfra/export?days=30`
(1 to 30 days) returns the identities, the served totals and the history as a
JSON attachment, published or not.

## Storage

The snapshots are kept in the `openinfra_*` tables (revisions `0026_openinfra`
and `0027_openinfra_archives`):

- **Live rows**, one per host, GPU and engine, overwritten by every snapshot.
  A snapshot marks a GPU degraded at 85 °C or with ECC errors, and an engine
  with more than 2000 queued requests or its GPUs at 95 %. Anything silent for
  90 seconds reads as offline.
- **Samples**, at most one per host, GPU and engine each second: the first
  snapshot in each second is kept. The served totals add up each sample's rates
  over that spacing. Samples from a release that kept one every 6 seconds stay,
  each moved to its own second.
- **Rollups** for 20-minute and hourly buckets, refreshed by the gateway's
  scheduler one minute past every fifth minute, on one replica at a time. A
  rollup keeps the means of its bucket's samples and the sums the served totals
  need.
- **Request events**, as the reporter reads them from the engine logs, each
  stored once however often it is sent.

A sample row takes roughly 200 bytes for a GPU and 400 for an engine, index
included, and each one gets 86,400 a day: a host with eight GPUs and two engines
adds about 200 MB a day, 1.5 GB in the week the database keeps. Rollups add a
few megabytes a year per host and are kept.

## Retention and weekly archives

Samples and request events older than `OPENINFRA_RETENTION_DAYS` (7 by
default) are deleted by the maintenance job that refreshes the rollups, a few
minutes' worth at a time; 0 keeps everything. Live rows and rollups are kept,
so the 24-hour and 7-day views reach back past the retention.

Before they go, each week is archived to `OPENINFRA_ARCHIVE_DIR`, a directory
on the gateway's own filesystem and so an environment setting, not one under
Admin. A job of its own, seven minutes past every hour, writes each week once it
has ended (Monday 00:00 UTC) and an hour has passed, to a directory named for
its Monday:

```text
<OPENINFRA_ARCHIVE_DIR>/2026-11-16/
  manifest.json                    the week, and each file's rows and bytes
  openinfra_gpu_samples.csv.gz     one gzip-compressed CSV per table, with a header row
  openinfra_engine_samples.csv.gz
  openinfra_cpu_samples.csv.gz
  openinfra_request_events.csv.gz
  openinfra_gpu_rollups.csv.gz     the week's 20-minute and hourly rollups
  ...
  openinfra_gpus.csv.gz            the hosts, GPUs and engines when it was written
```

- **Nothing is deleted before it is archived.** While `OPENINFRA_ARCHIVE_DIR`
  is set, only weeks already archived are deleted, so a week stays in the
  database until it is on disk, however long the directory cannot be written.
  Without it nothing is deleted, unless `OPENINFRA_DELETE_UNARCHIVED` (an
  Integrations setting, off by default) is on: then rows past the retention are
  deleted unarchived.
- **A week's directory is whole or absent.** It is written to a hidden
  directory beside it and renamed into place once complete, then recorded in
  `openinfra_archives` with the host that wrote it. A week with nothing in it
  is skipped. Each table is read a host, GPU or engine at a time, in short
  transactions, so a long archive holds back neither vacuum nor the rollups.
- **Docker Compose** sets it to `/app/var/data/openinfra-archive`, which is
  `var/data/openinfra-archive` in the deployment's checkout, outside the
  container. Set it to an empty value in the overlay to turn archiving off. A
  week of a host with eight GPUs and two engines compresses to roughly 200 MB;
  move old weeks elsewhere as you would any backup.
- **More than one replica**: the job runs on whichever replica holds its lock,
  so point the setting at storage every replica shares, or run one gateway.

`python -m serving.openinfra.archive` lists the weeks archived so far; with
`--run` it refreshes the rollups, archives and deletes once, as the jobs do. Run
it where the gateway runs, with its `DB_*` settings and `OPENINFRA_ARCHIVE_DIR`.

The files are plain CSV: times are milliseconds since the Unix epoch, and a
sample's `bucket` is its `ts` divided by 1000. To look at a week again, read
the files directly, or load one into a table of the same shape:

```bash
gunzip -c openinfra_gpu_samples.csv.gz | psql -c "\copy openinfra_gpu_samples FROM STDIN CSV HEADER"
```

## Moving from the Cloudflare Worker

OpenInfra was a Cloudflare Worker with its own D1 database before it moved
into the gateway. The snapshot format is the same; only the address changes.

1. Deploy a gateway release that has OpenInfra and set
   `OPENINFRA_INGEST_TOKEN`. Reusing the Worker's `INGEST_TOKEN` means the hosts
   need only the new address.
2. On each host, replace the reporter with `ops/openinfra/reporter.py` (it posts
   to `/internal/openinfra/ingest`, not `/api/ingest`) and point
   `OPENINFRA_URL` at the gateway. Check that snapshots arrive with
   `GET /admin/openinfra/export?days=1` before publishing.
3. Export the D1 database and import it, from an environment with the gateway's
   `DB_*` settings, such as the backend container:

   ```bash
   npx wrangler d1 export <database> --remote --output openinfra.sql
   python -m serving.openinfra.import_d1 openinfra.sql --dry-run
   python -m serving.openinfra.import_d1 openinfra.sql
   ```

   What the gateway already holds wins: a host, GPU or engine row or a rollup
   bucket it has is kept, and samples and request events are imported only
   from before each host's, GPU's or engine's first one in the gateway, and
   never into a week already archived; it holds the archive job's lock while it
   runs. The import can therefore run after the switch, and again. Imported
   samples older than the retention are archived and deleted by the jobs' next
   runs.
4. List the engines the Worker had retired in `OPENINFRA_RETIRED_ENGINES`.
5. Turn on `features.openinfra`, send the old dashboard's address to
   `/openinfra`, and retire the Worker.

## Limitations

- A reporter keeps nothing while the gateway is unreachable.
- The ingest endpoint has no rate limit of its own; the token is the gate, so
  make it long and random.
- The served totals are estimates: they add up each sample's rates rather than
  count requests, and the last day is summed from 20-minute rollups, each
  bucket's mean rate over the part of it inside the day. A bucket in which a
  reporter started or stopped counts as if it had reported throughout.
- The page's finest points are 5-second means. The per-second samples are in
  the database for the retention and in the weekly archives.
- A request event that reaches the gateway more than 15 minutes after it
  happened is dropped.
