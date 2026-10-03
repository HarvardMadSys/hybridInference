"""Launch a metered experiment over SSH without persisting provider credentials."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

_FETCH = r"""
import json, re, sys
from pathlib import Path
values = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)', line)
    if match and match[1] in {'DEEPSEEK_BASE_URL', 'DEEPSEEK_API_KEY'}:
        value = match[2].strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        values[match[1]] = value
if not values.get('DEEPSEEK_API_KEY') or not values.get('DEEPSEEK_BASE_URL'):
    raise SystemExit('Missing configured provider credentials')
print(json.dumps(values))
"""

_INSPECT = r"""
import json, sys, urllib.request
from urllib.parse import urlsplit
config = json.load(sys.stdin)
base = config['DEEPSEEK_BASE_URL'].rstrip('/')
if urlsplit(base).scheme != 'https': raise SystemExit('HTTPS required')
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
opener = urllib.request.build_opener(NoRedirect())
result = {'base_url': base}
for name, path in [('models', '/models'), ('balance', '/user/balance')]:
    request = urllib.request.Request(base + path, headers={
        'Authorization': 'Bearer ' + config['DEEPSEEK_API_KEY']})
    with opener.open(request, timeout=20) as response:
        data = json.loads(response.read())
        if name == 'balance':
            data = {'is_available': data.get('is_available'),
                    'currencies': [item.get('currency') for item in data.get('balance_infos', [])]}
        result[name] = data
print(json.dumps(result))
"""

_RUN = r"""
import datetime, json, os, shlex, subprocess, sys, urllib.request
from pathlib import Path
from urllib.parse import urlsplit
request = json.load(sys.stdin)
config = json.loads(Path(request['config']).read_text())
expected = urlsplit(request['credentials']['DEEPSEEK_BASE_URL'])
actual = urlsplit(config['cloud']['base_url'])
if actual.scheme != 'https' or (actual.hostname, actual.port) != (expected.hostname, expected.port):
    raise SystemExit('Cloud endpoint does not match configured credential destination')
if config['policy'] != 'all_api' and config['local'].get('expected_tensor_parallel'):
    endpoint = urlsplit(config['local']['base_url'])
    if endpoint.hostname not in {'127.0.0.1', 'localhost'}:
        raise SystemExit('Local topology check requires a loopback model endpoint')
    observed = {'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'expected_tensor_parallel': config['local']['expected_tensor_parallel']}
    matches = []
    for line in subprocess.check_output(['ps', '-eo', 'args'], text=True).splitlines():
        if 'vllm.entrypoints.openai.api_server' not in line: continue
        try:
            args = shlex.split(line)
            if args[args.index('--port') + 1] != str(endpoint.port): continue
            matches.append(int(args[args.index('--tensor-parallel-size') + 1]))
        except (ValueError, IndexError): continue
    observed['observed_tensor_parallel'] = matches
    base = f'{endpoint.scheme}://{endpoint.netloc}'
    with urllib.request.urlopen(base + '/metrics', timeout=10) as response:
        metrics = response.read().decode()
    counts = [float(line.rsplit(' ', 1)[1]) for line in metrics.splitlines()
              if not line.startswith('#') and any(k in line for k in
                  ['num_requests_running{', 'num_requests_waiting{'])]
    observed['engine_request_counts'] = counts
    with urllib.request.urlopen(base + '/v1/models', timeout=10) as response:
        observed['models'] = json.load(response)
    with Path(request['output'] + '.preflight.json').open('x') as handle:
        json.dump(observed, handle, indent=2)
    if matches != [config['local']['expected_tensor_parallel']]:
        raise SystemExit('Local model parallelism differs from experiment configuration')
    if len(counts) < 2 or any(count != 0 for count in counts):
        raise SystemExit('Local model queues are not idle; no experiment was dispatched')
root = Path(request['code']).resolve()
os.chdir(root)
os.environ['PYTHONPATH'] = str(root / 'apps/backend') + os.pathsep + str(root)
if request.get('tokenizer_cache_dir'):
    os.environ['TIKTOKEN_CACHE_DIR'] = request['tokenizer_cache_dir']
os.environ['NIMBUS_DEEPSEEK_API_KEY'] = request['credentials']['DEEPSEEK_API_KEY']
args = [sys.executable, '-m', 'benchmark.nimbus.runner', '--config', request['config'],
        '--workload', request['workload'], '--output', request['output'],
        '--ledger', request['ledger'], '--api-key-env', 'NIMBUS_DEEPSEEK_API_KEY']
os.execv(sys.executable, args)
"""


def ssh_prefix(host: str, port: int | None = None) -> list[str]:
    """Return a noninteractive SSH command with inherited forwards disabled."""
    if host.startswith("-") or any(char.isspace() for char in host):
        raise ValueError("invalid SSH host alias")
    result = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "LogLevel=ERROR",
    ]
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid SSH port")
        result += ["-p", str(port)]
    return [*result, host]


def main() -> None:
    """Fetch only the required credential fields, then forward them in SSH stdin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("action", choices=["inspect", "run"])
    for name in ("code", "config", "workload", "output", "ledger"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    connection = json.loads(args.connection.read_text())
    source_command = shlex.join(["python3", "-c", _FETCH, connection["credential_env_file"]])
    source = subprocess.run(
        [*ssh_prefix(connection["credential_ssh_target"]), source_command],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if source.returncode:
        raise SystemExit("Could not read existing provider configuration over SSH")
    credentials = json.loads(source.stdout)
    payload = credentials
    script = _INSPECT
    if args.action == "run":
        missing = [
            name
            for name in ("code", "config", "workload", "output", "ledger")
            if not getattr(args, name)
        ]
        if missing:
            parser.error("run requires: " + ", ".join(missing))
        payload = {
            name: getattr(args, name) for name in ("code", "config", "workload", "output", "ledger")
        }
        payload["credentials"] = credentials
        payload["tokenizer_cache_dir"] = connection.get("tokenizer_cache_dir")
        script = _RUN
    command = shlex.join([connection.get("python", "python3"), "-c", script])
    process = subprocess.run(
        [*ssh_prefix(connection["ssh_target"], connection.get("ssh_port")), command],
        input=json.dumps(payload).encode(),
        check=False,
    )
    raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
