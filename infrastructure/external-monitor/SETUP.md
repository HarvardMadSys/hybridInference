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

# Create data directories
sudo mkdir -p /var/lib/ext-prometheus /var/lib/ext-alertmanager

# Install systemd services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ext-blackbox-exporter ext-prometheus ext-alertmanager
```

## Verify

```bash
# Probe is working (value should be 1)
curl -s 'http://127.0.0.1:9090/api/v1/query?query=probe_success' | python3 -m json.tool

# Alertmanager is healthy
curl -s http://127.0.0.1:9093/-/healthy

# Alert rules loaded
curl -s http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```
