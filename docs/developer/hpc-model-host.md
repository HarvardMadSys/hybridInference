# Serving a Model from an HPC Cluster

Many self-hosters have GPUs, but not GPUs they own outright: the hardware sits
behind a batch scheduler (Slurm, PBS, LSF) that hands out a compute node for a
few hours at a time. This page covers running an OpenAI-compatible model server
on such a node and attaching it to the gateway.

Three things make this different from the fixed-host case in
[Adding a New Local Model](add-local-model.md):

- **the node is ephemeral.** You get a different hostname on every allocation,
  and the job ends whether or not you are finished with it;
- **the node is usually not routable** from wherever the gateway runs, and often
  cannot reach a container registry either;
- **the allocation costs someone money or queue priority** for as long as you
  hold it, so releasing it cleanly is part of the procedure, not an afterthought.

The shape that follows from this: the model server binds loopback on the compute
node, an SSH reverse tunnel carries it to a **fixed** loopback port on the
gateway host, and the gateway's route names that fixed port. The node's changing
hostname then never appears in the model registry, and a new allocation is
picked up by restarting the tunnel rather than by editing config.

```text
compute node (new hostname each allocation)        gateway host
┌──────────────────────────────┐                   ┌──────────────────────────┐
│ vLLM in a container          │  ssh -R           │ 127.0.0.1:8001           │
│ published on 127.0.0.1:8000  │ ────────────────► │   ▲                      │
└──────────────────────────────┘                   │   │ base_url             │
                                                   │ gateway                  │
                                                   └──────────────────────────┘
```

Every scheduler flag, path and port below is a placeholder. Cluster policy —
partition names, GPU resource names, walltime limits, which container runtime is
installed — is site-specific, and there is no portable default.

## 1. Allocate a node

With Slurm, an interactive allocation looks like this. Fill in the placeholders
from your site's documentation:

```bash
salloc \
  --partition=<gpu-partition> \
  --gres=gpu:<count> \
  --cpus-per-task=<cpus> \
  --mem=<memory> \
  --time=<hh:mm:ss>
```

Many sites also ship a local wrapper around this; use whichever your
documentation names. Note the job ID and the allocated node name — you need the
first to release the allocation and the second to log in:

```bash
squeue --me
ssh <allocated-node>
```

The node name changes on every allocation. Do not put it in the model registry.

## 2. Make the container image available offline

Compute nodes frequently have no outbound network, so pulling the serving image
on the node fails. Pull it once somewhere that does have network, export it to
shared storage that the compute nodes can read, and import it there.

Pin an exact tag (or a digest) rather than `:latest`. `:latest` means a
different image every time you export, which turns "it worked last week" into an
unreproducible report.

```bash
# On a host with registry access
podman pull docker.io/vllm/vllm-openai:<version>
podman save --output /path/to/shared/vllm-openai-<version>.tar \
  docker.io/vllm/vllm-openai:<version>

# On the compute node
podman load --input /path/to/shared/vllm-openai-<version>.tar
podman images
```

`docker save` / `docker load` take the same arguments if that is the runtime
your site provides.

## 3. Start the model server

```bash
podman run --rm \
  --name model-server \
  --device nvidia.com/gpu=all \
  --ipc=host \
  --publish 127.0.0.1:8000:8000 \
  --volume /path/to/model-weights:/models:Z \
  docker.io/vllm/vllm-openai:<version> \
  --model /models/<model-directory> \
  --served-model-name <served-model-name> \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size <gpu-count> \
  --gpu-memory-utilization 0.95
```

Points worth understanding rather than copying:

- **`--publish 127.0.0.1:8000:8000`** keeps the server off the cluster's
  internal network. A bare `-p 8000:8000` publishes on every interface of a node
  you share with other tenants, with no authentication in front of it. The
  reverse tunnel in the next step is all the reachability you need.
- **`--host 0.0.0.0`** is the *container's* interface, not the node's. The
  process must listen on the container's external interface for the loopback
  publish above to reach it; binding `127.0.0.1` inside the container would make
  the published port answer nothing.
- **`--tensor-parallel-size`** must match the number of GPUs visible in the
  allocation. Two GPUs, `2`. Requesting more shards than GPUs fails at startup.
- **`--ipc=host`** gives vLLM's tensor-parallel worker processes the host's
  shared-memory segment; the container default is too small for them. Set a
  large `--shm-size` instead if your site disallows `--ipc=host`.
- **`--device nvidia.com/gpu=all`** is Podman's CDI syntax. Docker uses
  `--gpus all`.
- **`:Z`** on the volume relabels the mount for SELinux and is Podman/Docker
  specific; drop it where SELinux is not enforced.
- **`--served-model-name`** gives the model a stable short id. Without it the
  served id is the filesystem path from `--model`, which is what the gateway
  would then have to send as `provider_model_id`.

Confirm on the node before going further:

```bash
curl -s http://127.0.0.1:8000/v1/models
```

## 4. Tunnel it to the gateway host

From the compute node, open a reverse tunnel to a fixed loopback port on the
gateway host. Record the PID so you can close it later:

```bash
ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:8001:127.0.0.1:8000 \
  <user>@<gateway-host> &
echo $! > ~/model-tunnel.pid
```

- **Bind the remote end to `127.0.0.1`.** `-R 0.0.0.0:8001:...` asks the gateway
  host to publish an unauthenticated model server to every network it is on. It
  also only works if the gateway's `sshd` sets `GatewayPorts yes`; leaving that
  off is the safer configuration.
- **`ExitOnForwardFailure=yes`** makes the tunnel fail loudly when port 8001 on
  the gateway host is still held by a previous allocation's forwarding, instead
  of connecting and quietly forwarding nothing.
- The tunnel dies with the allocation. To ride out network blips, wrap the same
  command in `autossh`, a systemd user unit, or a shell retry loop — but nothing
  will survive the job ending.

Verify **from the gateway host**, which is where the gateway will resolve it:

```bash
curl -s http://127.0.0.1:8001/v1/models
```

## 5. Register the route

The gateway now sees an ordinary OpenAI-compatible server at
`http://127.0.0.1:8001/v1`. Registering it is not special —
[Adding a New Local Model](add-local-model.md) is the authority on the registry
entry, the `openai_compat` route fields, and verifying the model through the
public `/v1` API. Use the fixed tunnel port as `base_url` and your
`--served-model-name` as `provider_model_id`.

## 6. Release everything when you are done

Skipping this leaves a job burning its walltime, a dead route in the registry,
and a stale listener on the gateway host that will make the next allocation's
tunnel fail.

```bash
# 1. Remove or disable the route in the model registry, then restart the
#    gateway, so it stops sending traffic to a port that is about to close.
#    See add-local-model.md.

# 2. On the compute node: close the tunnel and stop the server.
kill "$(cat ~/model-tunnel.pid)" && rm ~/model-tunnel.pid
podman stop model-server

# 3. Release the allocation: exit the salloc shell, or from the login node
scancel <job-id>
```

Then confirm from the gateway host that the port is genuinely free — the
following should now fail to connect:

```bash
curl -s --max-time 5 http://127.0.0.1:8001/v1/models
```
