# MQTT Downstream — Standalone Docker

![MQTT Downstream](logo.png)

A standalone Docker container version of the [MQTT Downstream](https://github.com/maxandcheeses/ha-mqtt-downstream-addon) HA addon. Runs on any host with Docker — does not require Home Assistant OS or the Supervisor.

## Differences from the HA addon

| | HA Addon | Standalone Docker |
|---|---|---|
| Requires HA OS / Supervised | Yes | No |
| Auth to HA | Supervisor token (automatic) | Long-lived access token |
| HA WebSocket URL | `ws://supervisor/core/websocket` | `ws://<ha-ip>:8123/api/websocket` |
| Configuration | Addon options UI | Environment variables / `docker-compose.yml` |

## Prerequisites

- A running Home Assistant instance accessible on the network
- A Long-Lived Access Token from **HA Profile → Security → Long-Lived Access Tokens**
- An MQTT broker accessible to both this container and your downstream HA instance
- The `input_select` helpers created in HA (see main [README](https://github.com/maxandcheeses/ha-mqtt-downstream-addon))

## Quick start

1. Clone or copy this directory
2. Copy `.env.example` to `.env` and fill in your token, broker password, and (optionally) TLS certs
3. Edit `docker-compose.yml` — set `HA_WS_URL`, `BROKER_HOST`, and other non-sensitive options
4. Run:

```bash
docker compose up -d
```

4. Check logs:

```bash
docker compose logs -f
```

## Configuration

All configuration is via environment variables. Set them in `docker-compose.yml` or pass them with `docker run -e`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `HA_TOKEN` | ✅ | — | Long-lived access token from HA profile |
| `HA_WS_URL` | ✅ | `ws://homeassistant.local:8123/api/websocket` | WebSocket URL of your HA instance |
| `MQTT_BASE` | ✅ | — | Base MQTT topic (e.g. `homeassistant-guest`) |
| `DISCOVERY_PREFIX` | ✅ | `homeassistant-guest` | MQTT discovery prefix — must match downstream HA config |
| `BROKER_HOST` | ✅ | — | MQTT broker hostname or IP |
| `BROKER_USERNAME` | ✅ | — | MQTT broker username |
| `BROKER_PASSWORD` | ✅ | — | MQTT broker password |
| `BROKER_TLS_CA` | ❌ | — | Base64-encoded CA certificate (PEM). Required to enable TLS. See [docs/mqtt-tls.md](https://github.com/maxandcheeses/ha-mqtt-downstream-standalone/blob/main/docs/mqtt-tls.md) |
| `BROKER_TLS_CERT` | ❌ | — | Base64-encoded client certificate (PEM). Only required if your broker uses mutual TLS |
| `BROKER_TLS_KEY` | ❌ | — | Base64-encoded client private key (PEM). Only required if your broker uses mutual TLS |
| `ENTITIES_SELECT` | ⚠️ | `input_select.mqtt_downstream_entities` | `input_select` entity ID for entity glob patterns |
| `AREAS_SELECT` | ⚠️ | `input_select.mqtt_downstream_areas` | `input_select` entity ID for area names |
| `DOMAINS_SELECT` | ⚠️ | `input_select.mqtt_downstream_domains` | `input_select` entity ID for domain includes |
| `EXCLUDES_SELECT` | ❌ | `input_select.mqtt_downstream_excludes` | `input_select` entity ID for exclusion patterns |
| `BROKER_PORT` | ❌ | `1883` | MQTT broker port |
| `ENABLED_ENTITY` | ❌ | — | Binary HA entity to enable/disable publishing |
| `DISCOVERY_ON_STARTUP` | ❌ | `true` | Run discovery on container start |
| `DISCOVERY_ON_DROPDOWN_CHANGE` | ❌ | `true` | Run discovery when a config dropdown changes |
| `DISCOVERY_ON_BIRTH` | ❌ | `true` | Run discovery on MQTT birth message |
| `UNPUBLISH_ON_REMOVE` | ❌ | `true` | Clear discovery topic when entity is removed from list |
| `RETAIN` | ❌ | `true` | Publish with MQTT retain flag |
| `DEBUG` | ❌ | `false` | Enable verbose logging |

⚠️ = at least one of `ENTITIES_SELECT`, `AREAS_SELECT`, or `DOMAINS_SELECT` must be configured.

## Building manually

```bash
docker build -t mqtt-downstream .
docker run -d --name mqtt_downstream \
  --env-file .env \
  -e HA_WS_URL=ws://192.168.1.x:8123/api/websocket \
  -e MQTT_BASE=homeassistant-guest \
  -e DISCOVERY_PREFIX=homeassistant-guest \
  -e BROKER_HOST=192.168.1.x \
  -e BROKER_USERNAME=mqtt_downstream \
  -e ENTITIES_SELECT=input_select.mqtt_downstream_entities \
  --restart unless-stopped \
  mqtt-downstream
```

## TLS / Encrypted MQTT

Set `BROKER_TLS_CA` to enable TLS. See [docs/mqtt-tls.md](https://github.com/maxandcheeses/ha-mqtt-downstream-standalone/blob/main/docs/mqtt-tls.md) for the full guide.

```bash
# Convert certs to base64 (Linux)
export BROKER_TLS_CA=$(cat ca.crt | base64 -w 0)
export BROKER_TLS_CERT=$(cat client.crt | base64 -w 0)   # only if broker requires mutual TLS
export BROKER_TLS_KEY=$(cat client.key | base64 -w 0)    # only if broker requires mutual TLS
```

On macOS, omit `-w 0` (it is not supported): `cat ca.crt | base64`

Paste the base64 strings into `.env`. Leave `BROKER_TLS_CERT` and `BROKER_TLS_KEY` empty for standard one-way TLS — only `BROKER_TLS_CA` is required.

If `BROKER_TLS_CA` is not set, the container connects on plain TCP and logs a warning.

---

## Support

If this container saves you some time or adds value to your setup, consider buying me a home automation toy 🤖

[![Buy Me A Home Automation Toy](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png)](https://www.buymeacoffee.com/maxwellluong)