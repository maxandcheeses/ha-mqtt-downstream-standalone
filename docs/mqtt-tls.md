# Why You Should Encrypt Your MQTT Traffic

MQTT Downstream bridges your main Home Assistant to a downstream broker — often across network segments, VLANs, or even the internet via a tunnel. Without TLS, every message travels as plain text, including device states, commands, and credentials.

This doc explains the risks, how TLS helps, and how to get set up.

---

## What's at Stake

### Credentials in the clear
MQTT `CONNECT` packets carry your username and password. On an unencrypted connection, anyone with access to the network path can read them with a basic packet capture (`tcpdump`, Wireshark, etc.).

### State and command interception
Without encryption, an attacker on the same network can:
- Read every entity state you publish (presence, locks, alarms, sensors)
- Inject commands — turning lights on/off, unlocking doors, triggering scenes
- Replay captured packets to re-trigger actions

### Relevance to the guest network use case
MQTT Downstream is commonly used to push entity state to a guest or IoT VLAN. The whole point of that architecture is **network isolation** — but if your MQTT traffic crosses that boundary unencrypted, isolation only protects you against layer-3 routing attacks, not passive listeners on the wire.

---

## How TLS Helps

TLS (Transport Layer Security) wraps your MQTT connection in an encrypted tunnel. It provides:

- **Confidentiality** — packet contents are unreadable without the session keys
- **Integrity** — tampering with packets in transit is detectable
- **Authentication** — a CA cert verifies you're connecting to the right broker, not an impostor

Client certificates (mutual TLS) go further — the broker also verifies the client's identity, so even a valid password isn't enough to connect without the cert.

---

## Certificate Options

| Setup | What you need | Protects against |
|---|---|---|
| CA cert only | `broker_tls_ca` | Passive eavesdropping, MITM |
| CA + client cert | all three vars | Above + unauthorised clients |
| Self-signed CA | Your own CA | Fine for private networks |
| Public CA (Let's Encrypt) | Public cert on broker | Easiest for internet-facing brokers |

---

## Generating Certs (Self-Signed CA)

If your broker is on a private network, a self-signed CA is perfectly reasonable.

### 1. Create a CA

```bash
# Generate CA key and cert (valid 10 years)
openssl genrsa -out ca.key 4096
openssl req -new -x509 -days 3650 -key ca.key -out ca.crt \
  -subj "/CN=My MQTT CA"
```

### 2. Create a broker cert

```bash
openssl genrsa -out broker.key 2048
openssl req -new -key broker.key -out broker.csr \
  -subj "/CN=your-broker-hostname-or-ip"
openssl x509 -req -days 3650 -in broker.csr \
  -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out broker.crt
```

> **Important:** the CN (or a SAN) must match the hostname or IP you use for `broker_host`. If they don't match, TLS will reject the connection.

### 3. (Optional) Create a client cert

```bash
openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr \
  -subj "/CN=mqtt-downstream"
openssl x509 -req -days 3650 -in client.csr \
  -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt
```

### 4. Configure your broker (Mosquitto example)

```
# mosquitto.conf
listener 8883
cafile   /etc/mosquitto/certs/ca.crt
certfile /etc/mosquitto/certs/broker.crt
keyfile  /etc/mosquitto/certs/broker.key

# Require client certs (optional but recommended)
require_certificate true
use_identity_as_username true
```

---

## Converting Certs to Base64 for MQTT Downstream

MQTT Downstream accepts certs as base64-encoded strings so they can be stored safely in the addon config or as environment variables without needing file mounts.

```bash
# CA cert (required for TLS)
cat ca.crt | base64 -w 0
# → paste this as broker_tls_ca

# Client cert (only needed if broker requires mutual TLS)
cat client.crt | base64 -w 0
# → paste this as broker_tls_cert

# Client key (only needed if broker requires mutual TLS)
cat client.key | base64 -w 0
# → paste this as broker_tls_key
```

The `-w 0` flag disables line wrapping so you get a single continuous string.

On macOS, use `base64` without `-w`:

```bash
cat ca.crt | base64
```

---

## Configuring MQTT Downstream

### HA Addon (config.yaml / UI)

| Option | Value |
|---|---|
| `broker_host` | Your broker's hostname or IP |
| `broker_port` | `8883` (standard TLS port) |
| `broker_tls_ca` | base64 string from `cat ca.crt \| base64 -w 0` |
| `broker_tls_cert` | base64 string from `cat client.crt \| base64 -w 0` *(if using mutual TLS)* |
| `broker_tls_key` | base64 string from `cat client.key \| base64 -w 0` *(if using mutual TLS)* |

### Standalone Docker (`docker-compose.yml`)

```yaml
BROKER_HOST: "your-broker-hostname"
BROKER_PORT: "8883"
BROKER_TLS_CA: "LS0tLS1CRUdJTi..."     # output of: cat ca.crt | base64 -w 0
BROKER_TLS_CERT: ""                     # leave empty if not using mutual TLS
BROKER_TLS_KEY: ""                      # leave empty if not using mutual TLS
```

---

## What Happens Without Certs

If you leave the TLS fields blank, MQTT Downstream will connect on plain TCP and log this warning at startup:

```
[WARNING] MQTT TLS is NOT configured — broker traffic is unencrypted.
See docs/mqtt-tls.md for why this matters and how to set up certs.
```

The addon will still work — the warning is there to make the choice explicit, not to block operation. If your broker is on a fully trusted, isolated network with no external exposure, unencrypted MQTT may be an acceptable trade-off.