# OpenInfra

OpenInfra is a public console page, `/openinfra`, with live metrics from the GPU
hosts and inference engines a deployment runs itself: GPU utilization, memory,
power, temperature and interconnect; host CPU and memory; and each SGLang or
vLLM engine's queue, KV cache, prefix-cache hits, throughput and latencies.
Metrics are sampled every second, with history from 15 minutes to 7 days.
The page also shows the requests and tokens served in the last hour and day,
and the token counts and latencies of recent requests, but never prompts,
completions, addresses or request ids.

OpenInfra is off by default. Collecting and publishing are turned on
separately, so a deployment can collect before it publishes.

## How the data gets there

A small reporter runs on each GPU host. Every second it reads the GPUs, the host
and the engines locally, and posts one JSON snapshot to the gateway over HTTPS.
The gateway stores the snapshots in its PostgreSQL database, and the page reads
them back.

```text
GPU host: reporter ──POST /internal/openinfra/ingest──▶ edge ──▶ console ──▶ gateway ──▶ PostgreSQL
            Authorization: Bearer <OPENINFRA_INGEST_TOKEN>

browser ──/openinfra──▶ console page ──GET /public-openinfra, /public-openinfra/history──▶ gateway
```

The GPU hosts need only outbound HTTPS: the gateway never connects to them. A
reporter can post to any address that reaches the gateway, the public site or
the backend's address on a private network, since the console only relays the
ingest route (see [The Public Path Table](public-path-table.md)). The routing
engine does not read these metrics.

## Turning it on

1. **Collect.** Set `OPENINFRA_INGEST_TOKEN` to a long random string, such as
   the output of `openssl rand -hex 32`. While it is empty,
   `POST /internal/openinfra/ingest` answers `404` and nothing is collected.
2. **Install a reporter** on each GPU host, as described in
   [Running a reporter on a GPU host](#running-a-reporter-on-a-gpu-host).
3. **Publish.** Add `"openinfra": true` to `features` in the
   `SITE_CONFIGURATION` document (see
   [Application documents](configuration.md#application-documents)). The
   header and footer then link to `/openinfra`, and the page's data endpoints
   answer. Without the flag they answer `404` and the page says "OpenInfra is
   not published for this site".

The OpenInfra settings are in **Admin → Settings → Site & Setup → Advanced
settings**, Integrations section, and apply within seconds:

| Setting | Default | Meaning |
|---|---|---|
| `OPENINFRA_INGEST_TOKEN` | empty (not collecting) | The bearer token reporters present |
| `OPENINFRA_ENGINE_ORDER` | empty | Engine names, comma-separated, in tab order. Unlisted engines follow, the one that served the most tokens in the last day first |
| `OPENINFRA_RETIRED_ENGINES` | empty | Engine ids, comma-separated, to leave out of the page, the served totals, the history and exports. Their data stays stored; remove the id to bring it back |
| `OPENINFRA_RETENTION_DAYS` | `7` | Days the per-second samples and request events stay in the database; `0` keeps everything. See [Retention and weekly archives](#retention-and-weekly-archives) |
| `OPENINFRA_DELETE_UNARCHIVED` | off | Delete rows past the retention even without an archive directory |

An engine that stops reporting stays on the page as offline until you retire
its id.

### Pointing another deployment at this page

A fleet reports to one gateway. Another deployment of the same fleet, such as a
staging site, can link to that gateway's page instead of collecting its own
copy: set `OPENINFRA_PUBLIC_URL` in its console (frontend container)
environment to the page's HTTPS address, such as
`https://gateway.example.org/openinfra`, and recreate the console. Its header
and footer then link there in a new tab, and its `/openinfra` redirects there.
It needs neither the `features.openinfra` flag nor an ingest token. A URL that
is not HTTPS, or that contains credentials, is ignored with a warning.

## Running a reporter on a GPU host

The reporter, `ops/openinfra/reporter.py`, uses only the Python standard
library, so the host's system Python runs it. It needs `nvidia-smi`. Copy
`ops/openinfra/` to the host and write a node configuration, starting from
`ops/openinfra/node.example.json`:

| Field | Meaning |
|---|---|
| `node_id` | The host's id. GPU ids are `<node_id>:<index>`, so each host needs its own |
| `hostname`, `region`, `cluster` | Shown on the page |
| `flush_interval` | Seconds between snapshots; 1 by default. The gateway keeps at most one sample a second |
| `log_interval` | Seconds between log lines for flushes and for a repeating failure; 60 by default, 0 logs every flush |
| `sglang` | The host's engines, SGLang and vLLM alike. `[]` means none; without the key the reporter scrapes a local SGLang on port 30000 |
| `sglang[].url` | Where to scrape the engine: SGLang's `/v1/loads` (preferred) or `/metrics`, or vLLM's `/metrics` |
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

`ops/openinfra/openinfra-reporter.service` runs the reporter under systemd, and
its header lists the install commands. The unit reads `OPENINFRA_URL`,
`OPENINFRA_TOKEN` and `OPENINFRA_NODE_CONFIG` from `/etc/openinfra/reporter.env`
(template: `ops/openinfra/reporter.env.example`). Keep that file readable only
by root and the reporter's group, because it holds the token.

What each source needs:

- **SGLang.** Start it with `--enable-metrics`. Without it, requests per
  minute, time to first token and time per output token come only from the
  container logs, once a minute. If an engine's `/v1/loads` requires its API
  key, point `url` at `/metrics`: the reporter sends no key.
- **vLLM.** It has no `/v1/loads`: point `url` at `/metrics` and set
  `"engine": "vllm"`.
- **Recent requests** come only from the engine's container logs, read once a
  minute, whatever counters the engine has. This needs `docker` (the docker
  group, or `sudo -n docker`); the unit's install commands add `openinfra` to
  the docker group. Without it, everything else is still reported, the page's
  recent requests stay empty, and the reporter says why once in its log
  (`journalctl -u openinfra-reporter | grep 'engine logs'`).
- **DCGM (optional).** Tensor-core, DRAM and NVLink charts need NVIDIA's
  dcgm-exporter, installed with the field list and override in
  `ops/openinfra/dcgm/`. The reporter reads `http://127.0.0.1:9400/metrics`
  when it answers. PCIe throughput comes from DCGM too, or from
  `nvidia-smi dmon` without it.

The reporter sends a `curl` User-Agent, because some edges refuse Python's
default; `OPENINFRA_USER_AGENT` overrides it. Command-line flags can replace
the configuration file: `--dashboard` (the gateway address), `--token`,
`--node-id` and the others that `--help` lists.

## The ingest API

`POST /internal/openinfra/ingest` takes one host's snapshot. The token goes in
`Authorization: Bearer <token>` or `x-ingest-token: <token>`:

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

- `ts`, optional, is the snapshot time in milliseconds by the reporter's clock.
  The gateway stores the snapshot at that time while it is within a minute of
  its own clock, and at its own time otherwise.
- `sglang` lists every engine on the host, vLLM included (`engine` says which).
  An engine sends parsed `metrics`, raw `prometheus` text, or both. Prometheus
  text that exposes several models gives each model its own engine,
  `<id>:<model>`.
- Unknown fields and values of the wrong type are ignored, so older reporters
  keep working. A body of the wrong shape is refused whole.
- A body may carry up to 64 GPUs and 16 engines, and up to 2 MiB. Only the
  first 20 request events per engine are kept. A request event more than 15
  minutes older or more than a minute newer than the snapshot is dropped and
  counted in `requests_dropped`.

| Answer | When |
|---|---|
| `200 {"ok": true, "gpus": N, "sglang": M}` | Stored, with `"requests_dropped": K` when request events were dropped |
| `400` | The body is not JSON, or not a snapshot; the message says why |
| `401` | The token is missing or wrong. It is checked before the body is read |
| `404` | No token is configured, so this deployment does not collect |
| `408`, `413` | The body was too slow or too large |
| `503` | No database, or the write failed. The reporter sends its next snapshot as usual |

## What the page reads

Both reads need no authentication, return only what the page shows, and answer
`404` unless the deployment publishes OpenInfra. However many people ask, the
gateway computes the dashboard at most once every 5 seconds and a history
window at most once per point spacing (up to 30 seconds), and sends a matching
`Cache-Control`.

- `GET /public-openinfra`: the fleet's state (`healthy`, `busy`, `degraded` or
  `offline`), fleet-wide figures, every host, GPU and engine (engines under
  `sglang`, in tab order), the requests and tokens served in the last hour and
  day, and each engine's 40 most recent requests.
- `GET /public-openinfra/history?hours=2&node=`: time series for every host, or
  for one, over the last `hours`, rounded up to one of 0.25, 0.5, 1, 2, 3, 6,
  12, 24, 48, 72 or 168. Each point is the mean of the per-second samples in
  it; `grain_ms` gives the spacing, from 5 seconds over 15 minutes to an hour
  over 2 days or more. A `node` that has never reported answers `404`.

Administrators can download the data with the **Export** button on the page, or
with `GET /admin/openinfra/export?days=30` (1 to 30 days): one JSON file with
the identities, the served totals and the history. The API answers whether or
not the page is published.

## Storage

The snapshots go to the `openinfra_*` tables:

- **Live rows**, one per host, GPU and engine, overwritten by each snapshot. A
  GPU is degraded at 85 °C or with ECC errors. An engine is degraded with more
  than 2000 queued requests, or when the utilization of one of its GPUs reaches
  95 %. Anything silent for 90 seconds shows as offline.
- **Samples**, at most one per host, GPU and engine each second. The first
  snapshot in each second is kept.
- **Rollups**: the means of the samples in 20-minute and hourly buckets,
  refreshed one minute past every fifth minute.
- **Request events** from the engine logs, each stored once.

A sample row takes about 200 bytes for a GPU and 400 for an engine, index
included, so a host with eight GPUs and two engines adds about 200 MB a day, or
1.5 GB over the default 7-day retention. Rollups add a few megabytes a year per
host and are kept.

## Retention and weekly archives

The job that refreshes the rollups also deletes samples and request events
older than `OPENINFRA_RETENTION_DAYS`, but only once their week is archived.
Live rows and rollups are kept, so the 24-hour and 7-day views reach back past
the retention.

The archives go to `OPENINFRA_ARCHIVE_DIR`, an environment variable rather than
an Admin setting because it is a path on the gateway's own filesystem. Docker
Compose sets it to `/app/var/data/openinfra-archive`, which is
`var/data/openinfra-archive` in the deployment's checkout. Set it to an empty
value in the overlay to turn archiving off; then nothing is deleted unless
`OPENINFRA_DELETE_UNARCHIVED` is on. With more than one replica, point it at
storage they all share, or run one gateway: the job runs on whichever replica
holds its lock.

At seven minutes past every hour, the archive job writes each week that ended
(Monday 00:00 UTC) more than an hour ago to a directory named for its Monday,
such as `<OPENINFRA_ARCHIVE_DIR>/2026-11-16/`. The directory holds one
gzip-compressed CSV per table, with a header row, and a `manifest.json` that
lists each file's rows and bytes. It appears only when complete, because it is
written under a hidden name first, and is then recorded in `openinfra_archives`.
A week of a host with eight GPUs and two engines compresses to about 200 MB;
move old weeks elsewhere as you would any backup.

`python -m serving.openinfra.archive` lists the weeks archived so far. With
`--run`, it refreshes the rollups, archives and deletes once, as the jobs do.
Run it where the gateway runs, with its `DB_*` settings and
`OPENINFRA_ARCHIVE_DIR`.

The files are plain CSV. Times are milliseconds since the Unix epoch, and a
sample's `bucket` is its `ts` divided by 1000. Read the files directly, or load
one into a table of the same shape:

```bash
gunzip -c openinfra_gpu_samples.csv.gz | psql -c "\copy openinfra_gpu_samples FROM STDIN CSV HEADER"
```

## Moving from the Cloudflare Worker

These steps move OpenInfra from a Cloudflare Worker with a D1 database into the
gateway. The snapshot format is the same; only the address changes.

1. Deploy a gateway release that has OpenInfra and set
   `OPENINFRA_INGEST_TOKEN`. If you reuse the Worker's `INGEST_TOKEN`, the
   hosts need only the new address.
2. On each host, replace the reporter with `ops/openinfra/reporter.py` (it posts
   to `/internal/openinfra/ingest`, not `/api/ingest`) and point
   `OPENINFRA_URL` at the gateway. Before publishing, check that snapshots
   arrive with `GET /admin/openinfra/export?days=1`.
3. Export the D1 database and import it from an environment with the gateway's
   `DB_*` settings, such as the backend container:

   ```bash
   npx wrangler d1 export <database> --remote --output openinfra.sql
   python -m serving.openinfra.import_d1 openinfra.sql --dry-run
   python -m serving.openinfra.import_d1 openinfra.sql
   ```

   The import never overwrites what the gateway already holds and never writes
   into an archived week, so you can run it after the switch, and again.
4. List the engines the Worker had retired in `OPENINFRA_RETIRED_ENGINES`.
5. Turn on `features.openinfra`, redirect the old dashboard's address to
   `/openinfra`, and retire the Worker.

## Limitations

- A reporter keeps nothing while the gateway is unreachable: the snapshots from
  that time are lost.
- The ingest endpoint has no rate limit of its own. The token is the only gate,
  so make it long and random.
- The served totals are estimates. They add up sampled rates rather than count
  requests, and the last day is summed from 20-minute rollups. A bucket in
  which a reporter started or stopped counts as if it had reported throughout.
- The page's finest points are 5-second means. The per-second samples stay in
  the database for the retention period, and in the weekly archives.
