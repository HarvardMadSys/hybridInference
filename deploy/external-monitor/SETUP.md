# External Monitoring Setup

Deploy on a separate server to detect whole-machine failures on the main server.

## Prerequisites

Download binaries (linux-amd64):
- [prometheus](https://github.com/prometheus/prometheus/releases)
- [alertmanager](https://github.com/prometheus/alertmanager/releases)
- [blackbox_exporter](https://github.com/prometheus/blackbox_exporter/releases)

Place all three in `/usr/local/bin/`.

## Install

```bash
# Copy config files
sudo mkdir -p /etc/external-monitor/rules
sudo cp blackbox.yml prometheus.yml alertmanager.yml /etc/external-monitor/
sudo cp rules/external_probe.yml /etc/external-monitor/rules/
sudo cp rules/latency_probe.yml /etc/external-monitor/rules/

# Create data directories
sudo mkdir -p /var/lib/ext-prometheus /var/lib/ext-alertmanager

# Install systemd services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ext-blackbox-exporter ext-prometheus ext-alertmanager
```

## Latency Prober

The latency prober sends a real streaming LLM request to freeinference every 5 minutes and
measures TTFT and output throughput. It exposes Prometheus metrics on `:9116/metrics`.

The prober implementation is sourced from the `llm-prober` submodule. This
repository keeps the Prometheus, Alertmanager, and systemd integration glue,
but does not keep a second in-tree copy of the prober source.

### Install

```bash
# Initialize the prober submodule in your checkout
git submodule update --init llm-prober

# Copy files from the llm-prober submodule
sudo mkdir -p /etc/external-monitor/latency-prober
sudo cp llm-prober/prober.py llm-prober/config.yml.example \
    llm-prober/requirements.txt \
    /etc/external-monitor/latency-prober/
sudo cp llm-prober/config.yml.example \
    /etc/external-monitor/latency-prober/config.yml

# Install Python dependencies (Python 3.10+ required)
pip3 install -r /etc/external-monitor/latency-prober/requirements.txt

# Edit config.yml to set the model name you want to probe.

# Create secrets file (holds the API key — never commit this)
sudo tee /etc/external-monitor/latency-prober.env > /dev/null <<'EOF'
FREEINFERENCE_API_KEY=hyi-your-key-here
EOF
sudo chmod 600 /etc/external-monitor/latency-prober.env

# Install and start the systemd service
sudo cp systemd/ext-latency-prober.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ext-latency-prober
```

### Test before enabling

```bash
# Run one probe cycle and print results to stdout, then exit
FREEINFERENCE_API_KEY=hyi-your-key-here \
    python3 /etc/external-monitor/latency-prober/prober.py \
    /etc/external-monitor/latency-prober/config.yml --run-once
```

### Verify

```bash
# Metrics are being scraped
curl -s http://127.0.0.1:9116/metrics | grep llm_probe

# Tell Prometheus to reload its config (picks up the new scrape job)
curl -s -X POST http://127.0.0.1:9090/-/reload

# Confirm the new job appears
curl -s 'http://127.0.0.1:9090/api/v1/targets' | python3 -m json.tool | grep llm_latency
```

---

## Verify (blackbox)

```bash
# Probe is working (value should be 1)
curl -s 'http://127.0.0.1:9090/api/v1/query?query=probe_success' | python3 -m json.tool

# Alertmanager is healthy
curl -s http://127.0.0.1:9093/-/healthy

# Alert rules loaded
curl -s http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```
