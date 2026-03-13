"""
MQTT Downstream — HA Add-on
Connects to HA via WebSocket and a downstream MQTT broker (system default or custom).
- Publishes MQTT discovery, state, and attributes for entities in a configured input_select
- Optionally filters by domain via a second input_select
- Routes inbound MQTT commands back to HA services
- Re-runs discovery on startup, dropdown changes, and MQTT birth messages
"""

import asyncio
import base64
import ssl
import tempfile
import fnmatch
import json
import logging
import os
import paho.mqtt.client as mqtt
import websockets

from domains import (
    mqtt_domain, entity_slug, discovery_domain, format_state,
    get_attribute_payloads, discovery_payload, resolve_command
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# Upgrade to DEBUG level after config is parsed — done below after DEBUG is set

# ── Config ────────────────────────────────────────────────────────────────────
def _require(name: str, fallback: str = "") -> str:
    """Return env var value, using fallback for empty strings. Never raises."""
    return os.environ.get(name) or fallback

def _require_strict(name: str) -> str:
    """Return env var value. Exits with error if missing or empty."""
    val = os.environ.get(name, "").strip()
    if not val:
        log.error("Required config '%s' is missing or empty — cannot start", name)
        raise SystemExit(1)
    return val

MQTT_BASE           = _require_strict("MQTT_BASE")
DISCOVERY_PREFIX        = _require("DISCOVERY_PREFIX", "homeassistant")
DISCOVERY_ON_STARTUP    = os.environ.get("DISCOVERY_ON_STARTUP",  "true").lower() == "true"
DISCOVERY_ON_DROPDOWN_CHANGE   = os.environ.get("DISCOVERY_ON_DROPDOWN_CHANGE", "true").lower() == "true"
DISCOVERY_ON_BIRTH      = os.environ.get("DISCOVERY_ON_BIRTH",    "true").lower() == "true"
UNPUBLISH_ON_REMOVE     = os.environ.get("UNPUBLISH_ON_REMOVE",   "true").lower() == "true"
ENABLED_ENTITY   = _require("ENABLED_ENTITY")
ENTITIES_SELECT  = _require("ENTITIES_SELECT")
AREAS_SELECT     = _require("AREAS_SELECT")
EXCLUDES_SELECT  = _require("EXCLUDES_SELECT")
DOMAINS_SELECT   = _require("DOMAINS_SELECT")
BROKER_HOST      = _require("BROKER_HOST", "core-mosquitto")
BROKER_PORT      = int(_require("BROKER_PORT") or 1883)
BROKER_USERNAME  = _require("BROKER_USERNAME")
BROKER_PASSWORD  = _require("BROKER_PASSWORD")
BROKER_TLS_CA    = _require("BROKER_TLS_CA")    # base64-encoded CA cert (PEM)
BROKER_TLS_CERT  = _require("BROKER_TLS_CERT")  # base64-encoded client cert (PEM)
BROKER_TLS_KEY   = _require("BROKER_TLS_KEY")   # base64-encoded client key (PEM)
DEBUG            = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
RETAIN          = os.environ.get("RETAIN", "").lower() in ("1", "true", "yes")
BIRTH_TOPIC        = f"{MQTT_BASE}/status"
HEARTBEAT_TOPIC    = f"{MQTT_BASE}/status/heartbeat"
HEARTBEAT_INTERVAL_SECONDS = max(0.0, float(_require("HEARTBEAT_INTERVAL_SECONDS", "60") or 0))  # seconds (supports floats e.g. 0.3), 0 = disabled

if DEBUG:
    logging.getLogger().setLevel(logging.DEBUG)
    log.debug("Debug logging enabled")

# Support both addon (SUPERVISOR_TOKEN) and standalone Docker (HA_TOKEN + HA_WS_URL)
HA_TOKEN  = _require("HA_TOKEN") or _require("SUPERVISOR_TOKEN")
if not HA_TOKEN:
    log.error("No HA token found — set HA_TOKEN (standalone) or SUPERVISOR_TOKEN (addon)")
    raise SystemExit(1)
HA_WS_URL = _require("HA_WS_URL", "ws://supervisor/core/websocket")

# Validate: if entities_select is empty, domains_select is required
if not ENTITIES_SELECT and not AREAS_SELECT and not DOMAINS_SELECT:
    log.error(
        "At least one of 'entities_select', 'areas_select', or 'domains_select' must be configured — cannot start."
        " 'domains_select' includes all entities of the listed domains."
    )
    raise SystemExit(1)


class MQTTDownstream:

    def __init__(self):
        self.mqttc   = None
        self.ws      = None
        self._msg_id = 0
        self.states: dict[str, dict] = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._discovery_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resolved_entities: list[str] = []  # expanded at startup
        self._previous_entities: list[str] = []  # used to detect removals
        self._area_entity_map: dict[str, list[str]] = {}  # area_name → [entity_ids]
        self._area_id_map: dict[str, str] = {}            # area_id → area_name

    # ── Entity list ───────────────────────────────────────────────────────────

    def _expand_entity_list(self):
        """Resolve entity list from:
        1. entities_select glob patterns
        2. areas_select area names → all entities in those areas
        3. domains_select domain filter
        4. excludes_select glob exclusions
        """
        all_ids  = list(self.states.keys())
        areas    = self.area_filter
        excludes = self.exclude_patterns
        resolved = []
        seen     = set()

        # ── Step 1: entities_select ──
        options = (
            self.states.get(ENTITIES_SELECT, {})
                .get("attributes", {})
                .get("options") or []
        )
        for pattern in options:
            if not pattern or pattern == "none":
                continue
            if any(c in pattern for c in ("*", "?", "[")):
                matches = fnmatch.filter(all_ids, pattern)
                for eid in sorted(matches):
                    if eid not in seen:
                        resolved.append(eid)
                        seen.add(eid)
                if matches:
                    log.info("Glob %r expanded to: %s", pattern, ", ".join(sorted(matches)))
                else:
                    log.warning("Glob %r matched no entities", pattern)
            else:
                if pattern not in seen:
                    resolved.append(pattern)
                    seen.add(pattern)

        # ── Step 2: areas_select ──
        if areas:
            # Normalise: if a requested value matches an area_id, resolve it to the area name
            def _resolve_area(requested: str) -> str:
                """Return the area name for a given input (accepts name or area_id, case-insensitive)."""
                req_lower = requested.lower()
                # Check by name first
                for name in self._area_entity_map:
                    if name.lower() == req_lower:
                        return name
                # Fall back: check area_id lookup
                for area_id, name in self._area_id_map.items():
                    if area_id.lower() == req_lower:
                        return name
                return ""

            known_names = {name.lower() for name in self._area_entity_map}
            known_ids   = {aid.lower() for aid in self._area_id_map}
            for requested in areas:
                if requested.lower() not in known_names and requested.lower() not in known_ids:
                    log.warning("Area %r not found in HA (tried as name and area_id) — no entities added", requested)
            for requested in areas:
                resolved_name = _resolve_area(requested)
                if resolved_name and resolved_name in self._area_entity_map:
                    for eid in self._area_entity_map[resolved_name]:
                        if eid not in seen:
                            resolved.append(eid)
                            seen.add(eid)
                            log.debug("Area %r added entity %s", resolved_name, eid)

        # ── Step 3: domains_select — add all entities of listed domains ──
        domains = self.domain_include
        if domains:
            for eid in all_ids:
                if eid not in seen and eid.split(".")[0] in domains:
                    resolved.append(eid)
                    seen.add(eid)
            log.debug("Domain include added entities for domains: %s", ", ".join(sorted(domains)))

        # ── Step 4: exclusions ──
        if excludes:
            excluded = set()
            for pattern in excludes:
                matches = fnmatch.filter(resolved, pattern)
                if not matches:
                    log.warning("Exclude pattern %r matched no entities", pattern)
                excluded.update(matches)
            # Warn if any entity explicitly listed in entities_select is being excluded
            explicit_entities = set(
                e for e in (
                    self.states.get(ENTITIES_SELECT, {})
                        .get("attributes", {})
                        .get("options") or []
                )
                if e and e != "none" and not any(c in e for c in ("*", "?", "["))
            )
            shadowed = explicit_entities & excluded
            if shadowed:
                log.warning(
                    "The following entities are explicitly listed in entities_select "
                    "but are also matched by an exclude pattern: %s",
                    ", ".join(sorted(shadowed))
                )
            if excluded:
                log.info("Excluded entities: %s", ", ".join(sorted(excluded)))
            resolved = [e for e in resolved if e not in excluded]

        removed = [e for e in self._previous_entities if e not in set(resolved)]
        self._previous_entities = list(resolved)
        self._resolved_entities = resolved
        log.info("Entity list resolved: %d entities", len(resolved))
        if DEBUG:
            log.info("── Entity list ──────────────────────────")
            for eid in resolved:
                domain = mqtt_domain(eid)
                log.info("  %s  (mqtt domain: %s)", eid, domain)
            if self.domain_include:
                log.info("── Domain include ───────────────────────")
                for d in sorted(self.domain_include):
                    log.info("  %s", d)
            log.info("─────────────────────────────────────────")
        if removed:
            log.info("Entities removed from list: %s", ", ".join(removed))
            if UNPUBLISH_ON_REMOVE:
                for entity_id in removed:
                    self._unpublish_discovery(entity_id)
            else:
                log.debug("unpublish_on_remove is disabled — discovery topics retained for: %s", ", ".join(removed))

    @property
    def entity_list(self) -> list[str]:
        return self._resolved_entities

    @property
    def domain_include(self) -> set[str]:
        if not DOMAINS_SELECT:
            return set()
        options = (
            self.states.get(DOMAINS_SELECT, {})
                .get("attributes", {})
                .get("options") or []
        )
        return {d.strip() for d in options if d and d != "none"}

    @property
    def area_filter(self) -> set[str]:
        if not AREAS_SELECT:
            return set()
        options = (
            self.states.get(AREAS_SELECT, {})
                .get("attributes", {})
                .get("options") or []
        )
        return {a.strip().lower() for a in options if a and a != "none"}

    @property
    def exclude_patterns(self) -> list[str]:
        if not EXCLUDES_SELECT:
            return []
        options = (
            self.states.get(EXCLUDES_SELECT, {})
                .get("attributes", {})
                .get("options") or []
        )
        return [p.strip() for p in options if p and p != "none"]

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send(self, msg: dict) -> dict:
        mid = self._next_id()
        msg["id"] = mid
        fut = self._loop.create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(msg))
        return await fut

    async def _ws_reader(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            mid = msg.get("id")
            if mid and mid in self._pending:
                self._pending.pop(mid).set_result(msg)
            elif msg.get("type") == "event":
                event = msg.get("event", {})
                if event.get("event_type") == "state_changed":
                    asyncio.create_task(
                        self._handle_state_changed(event.get("data", {}))
                    )

    async def _authenticate(self):
        msg = json.loads(await self.ws.recv())
        assert msg["type"] == "auth_required"
        await self.ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        msg = json.loads(await self.ws.recv())
        assert msg["type"] == "auth_ok", f"Auth failed: {msg}"
        log.info("WebSocket authenticated")

    async def _fetch_all_states(self):
        result = await self._send({"type": "get_states"})
        self.states = {s["entity_id"]: s for s in (result.get("result") or [])}
        log.info("Loaded %d states from HA", len(self.states))
        self._warn_missing_selects()
        if not self._is_enabled():
            log.info("Enabled entity (%s) is OFF at startup — starting in disabled state", ENABLED_ENTITY)
        else:
            self._expand_entity_list()

    def _warn_missing_selects(self):
        for name, entity_id in [
            ("enabled_entity",  ENABLED_ENTITY),
            ("entities_select", ENTITIES_SELECT),
            ("areas_select",    AREAS_SELECT),
            ("domains_select",  DOMAINS_SELECT),
            ("excludes_select", EXCLUDES_SELECT),
        ]:
            if entity_id and entity_id not in self.states:
                log.warning(
                    "Config '%s' is set to %r but that entity does not exist in HA",
                    name, entity_id
                )

    async def _fetch_area_entity_map(self):
        """Build a map of area_name -> [entity_ids] using the HA entity and device registry.

        Area assignment can live on the entity itself OR on its parent device.
        The entity-level assignment takes precedence; if the entity has no area_id,
        fall back to the device's area_id.
        """
        # Fetch areas
        areas_result = await self._send({"type": "config/area_registry/list"})
        areas = {a["area_id"]: a["name"] for a in (areas_result.get("result") or [])}
        log.debug("Area registry: %d areas", len(areas))

        # Fetch device registry — build device_id → area_id map
        devices_result = await self._send({"type": "config/device_registry/list"})
        device_area: dict[str, str] = {}
        for dev in (devices_result.get("result") or []):
            if dev.get("area_id"):
                device_area[dev["id"]] = dev["area_id"]

        # Fetch entity registry — resolve area from entity first, then device
        entities_result = await self._send({"type": "config/entity_registry/list"})
        entity_map: dict[str, list[str]] = {}
        for entry in (entities_result.get("result") or []):
            area_id = entry.get("area_id") or device_area.get(entry.get("device_id") or "")
            if area_id and area_id in areas:
                area_name = areas[area_id]
                entity_map.setdefault(area_name, []).append(entry["entity_id"])

        self._area_entity_map = entity_map
        # Build area_id → area_name lookup for matching by either id or name
        self._area_id_map = {area_id: name for area_id, name in areas.items()}
        log.info("Area map built: %d areas (%d total areas in registry)", len(entity_map), len(areas))
        if DEBUG:
            for area, eids in sorted(entity_map.items()):
                log.debug("  Area %r: %s", area, ", ".join(eids))

    async def _subscribe_events(self):
        await self._send({"type": "subscribe_events", "event_type": "state_changed"})
        log.info("Subscribed to state_changed events")

    async def _call_service(self, domain: str, service: str, entity_id: str, data: dict):
        await self._send({
            "type":         "call_service",
            "domain":       domain,
            "service":      service,
            "service_data": {"entity_id": entity_id, **data},
        })

    # ── MQTT ──────────────────────────────────────────────────────────────────

    def _setup_mqtt(self):
        self.mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if BROKER_USERNAME:
            self.mqttc.username_pw_set(BROKER_USERNAME, BROKER_PASSWORD)

        # ── TLS ───────────────────────────────────────────────────────────────
        if BROKER_TLS_CA:
            try:
                # Decode base64 certs into temp files — paho requires file paths
                self._tls_tmpfiles = []
                def _b64_to_tmp(b64: str, suffix: str) -> str:
                    data = base64.b64decode(b64)
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                    tmp.write(data)
                    tmp.flush()
                    self._tls_tmpfiles.append(tmp.name)
                    return tmp.name

                ca_path   = _b64_to_tmp(BROKER_TLS_CA,   ".ca.pem")
                cert_path = _b64_to_tmp(BROKER_TLS_CERT, ".cert.pem") if BROKER_TLS_CERT else None
                key_path  = _b64_to_tmp(BROKER_TLS_KEY,  ".key.pem")  if BROKER_TLS_KEY  else None
                self.mqttc.tls_set(
                    ca_certs    = ca_path,
                    certfile    = cert_path,
                    keyfile     = key_path,
                    tls_version = ssl.PROTOCOL_TLS_CLIENT,
                )
                log.info("MQTT TLS enabled (CA cert provided%s)",
                    ", client cert provided" if cert_path else "")
            except Exception as exc:
                log.error("Failed to configure MQTT TLS: %s — aborting", exc)
                raise SystemExit(1)
        else:
            log.warning(
                "MQTT TLS is NOT configured — broker traffic is unencrypted. "
                "See docs/mqtt-tls.md in the repo for why this matters and how to set up certs."
            )

        def on_connect(client, userdata, flags, reason_code, properties=None):
            if reason_code != 0:
                log.error("MQTT connection failed (rc=%s)", reason_code)
                return
            log.info("MQTT connected to %s:%s", BROKER_HOST, BROKER_PORT)
            if HEARTBEAT_INTERVAL_SECONDS > 0:
                client.publish(HEARTBEAT_TOPIC, payload="online", retain=True)
            client.subscribe(f"{MQTT_BASE}/+/+/set")
            client.subscribe(f"{MQTT_BASE}/+/+/#")
            client.subscribe(BIRTH_TOPIC)

        def on_disconnect(client, userdata, flags, reason_code, properties=None):
            log.warning("MQTT disconnected (rc=%s) — will auto-reconnect", reason_code)

        def on_message(client, userdata, msg):
            asyncio.run_coroutine_threadsafe(
                self._handle_mqtt_message(msg.topic, msg.payload.decode()),
                self._loop
            )

        self.mqttc.on_connect    = on_connect
        self.mqttc.on_disconnect = on_disconnect
        self.mqttc.on_message    = on_message
        self.mqttc.reconnect_delay_set(min_delay=1, max_delay=30)
        if HEARTBEAT_INTERVAL_SECONDS > 0:
            self.mqttc.will_set(HEARTBEAT_TOPIC, payload="offline", retain=True)
        self.mqttc.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
        self.mqttc.loop_start()


    # ── Publish ───────────────────────────────────────────────────────────────

    def _publish_heartbeat_discovery(self):
        """Publish MQTT discovery for the heartbeat binary sensor, then immediately publish state."""
        payload = {
            "name":             "MQTT Downstream",
            "unique_id":        f"{MQTT_BASE}_heartbeat",
            "state_topic":      HEARTBEAT_TOPIC,
            "payload_on":       "online",
            "payload_off":      "offline",
            "device_class":     "connectivity",
            "expire_after":     int(HEARTBEAT_INTERVAL_SECONDS * 3),
            "device": {
                "identifiers":  [f"{MQTT_BASE}_addon"],
                "name":         f"MQTT Downstream ({MQTT_BASE})",
                "manufacturer": "mqtt-downstream",
            },
        }
        self.mqttc.publish(
            f"{DISCOVERY_PREFIX}/binary_sensor/{MQTT_BASE}_heartbeat/config",
            json.dumps(payload),
            retain=True,
        )
        # Publish state immediately after discovery so HA receives it after subscribing
        self.mqttc.publish(HEARTBEAT_TOPIC, payload="online", retain=True)
        log.debug("Heartbeat discovery published")

    async def _heartbeat_loop(self):
        """Periodically publish online heartbeat. LWT handles offline on crash."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            self.mqttc.publish(HEARTBEAT_TOPIC, payload="online", retain=True)
            log.debug("Heartbeat published")

    def _publish_state(self, entity_id: str, state_obj: dict):
        state  = state_obj.get("state", "")
        attrs  = state_obj.get("attributes", {})
        domain = mqtt_domain(entity_id)
        slug   = entity_slug(entity_id)
        base   = f"{MQTT_BASE}/{domain}/{slug}"

        if domain == "timer":
            # Publish computed remaining as state when active, raw state otherwise
            attr_payloads = get_attribute_payloads(domain, attrs)
            if state == "active" and "remaining" in attr_payloads:
                state_value = attr_payloads["remaining"]
            elif state == "paused" and "remaining" in attr_payloads:
                state_value = f"paused ({attr_payloads['remaining']})"
            else:
                state_value = state  # idle
            self.mqttc.publish(f"{base}/state", state_value, retain=RETAIN)
            if attr_payloads:
                self.mqttc.publish(f"{base}/attributes", json.dumps(attr_payloads), retain=RETAIN)
        else:
            self.mqttc.publish(f"{base}/state", format_state(state, domain), retain=RETAIN)
            for subtopic, payload in get_attribute_payloads(domain, attrs).items():
                self.mqttc.publish(f"{base}/{subtopic}", payload, retain=RETAIN)

    def _publish_discovery(self, entity_id: str, state_obj: dict):
        domain  = mqtt_domain(entity_id)
        disc_domain = discovery_domain(entity_id)
        slug    = entity_slug(entity_id)
        payload = discovery_payload(entity_id, state_obj, MQTT_BASE, DISCOVERY_PREFIX)
        if payload is None:
            log.warning("No discovery payload for %s (domain=%s)", entity_id, domain)
            return
        self.mqttc.publish(
            f"{DISCOVERY_PREFIX}/{disc_domain}/{slug}/config",
            json.dumps(payload),
            retain=RETAIN
        )

    def _unpublish_discovery(self, entity_id: str):
        """Clear discovery for an entity by publishing an empty retained payload."""
        disc_domain = discovery_domain(entity_id)
        slug        = entity_slug(entity_id)
        topic       = f"{DISCOVERY_PREFIX}/{disc_domain}/{slug}/config"
        self.mqttc.publish(topic, "", retain=RETAIN)
        log.debug("Cleared discovery for %s", entity_id)

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def _run_discovery(self):
        entities = self.entity_list
        log.info("Running discovery for %d entities", len(entities))
        for entity_id in entities:
            state_obj = self.states.get(entity_id)
            if not state_obj:
                log.warning("No state for %s — skipping", entity_id)
                continue
            self._publish_discovery(entity_id, state_obj)
            self._publish_state(entity_id, state_obj)
            await asyncio.sleep(0.05)

    def _schedule_discovery(self):
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
        self._discovery_task = asyncio.create_task(self._run_discovery())

    # ── Event handlers ────────────────────────────────────────────────────────

    def _is_enabled(self) -> bool:
        """Return False if enabled_entity is configured and in a falsy state."""
        if not ENABLED_ENTITY:
            return True
        state = self.states.get(ENABLED_ENTITY, {}).get("state", "on")
        return state.lower() not in ("off", "false", "0", "unavailable", "unknown", "none", "")

    async def _handle_state_changed(self, data: dict):
        entity_id = data.get("entity_id", "")
        new_state = data.get("new_state")

        if new_state is None:
            return

        is_new = entity_id not in self.states
        self.states[entity_id] = new_state

        # Enabled entity toggled — start or stop publishing
        if ENABLED_ENTITY and entity_id == ENABLED_ENTITY:
            if self._is_enabled():
                log.info("Enabled entity turned ON (%s) — resuming", ENABLED_ENTITY)
                self._expand_entity_list()
                self._schedule_discovery()
            else:
                log.info("Enabled entity turned OFF (%s) — unpublishing discovery", ENABLED_ENTITY)
                for eid in list(self._resolved_entities):
                    self._unpublish_discovery(eid)
                self._resolved_entities = []
                self._previous_entities = []
            return

        # Skip all publishing if disabled
        if not self._is_enabled():
            return

        # Config dropdowns changed — re-expand entity list and re-run discovery
        if entity_id in (ENTITIES_SELECT, DOMAINS_SELECT, AREAS_SELECT, EXCLUDES_SELECT):
            log.debug("Config dropdown changed (%s) — re-expanding entity list", entity_id)
            self._expand_entity_list()
            if DISCOVERY_ON_DROPDOWN_CHANGE:
                self._schedule_discovery()
            return

        # New entity seen for the first time — re-expand in case it matches a glob
        if is_new:
            log.debug("New entity detected: %s — re-expanding entity list", entity_id)
            self._expand_entity_list()

        # Publish state if entity is in the resolved list
        if entity_id not in self.entity_list:
            return
        self._publish_state(entity_id, new_state)

    async def _handle_mqtt_message(self, topic: str, payload: str):
        # Birth message → re-run discovery
        if topic == BIRTH_TOPIC and payload.strip().lower() == "online":
            log.debug("MQTT birth message received")
            if DISCOVERY_ON_BIRTH:
                log.debug("Re-running discovery on birth message")
                self._schedule_discovery()
            return

        cmd = resolve_command(topic, payload, MQTT_BASE)
        if cmd is None:
            log.debug("Unrecognised topic: %s", topic)
            return
        if cmd["entity_id"] not in self.states:
            log.warning("Command for unknown entity %s — ignored", cmd["entity_id"])
            return

        log.debug("Command: %s.%s(%s) %s", cmd["domain"], cmd["service"], cmd["entity_id"], cmd["data"])
        await self._call_service(cmd["domain"], cmd["service"], cmd["entity_id"], cmd["data"])

    # ── Main ─────────────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._setup_mqtt()

        # Backoff schedule: 2s, 4s, 8s, 16s, 30s, 30s, ... (capped at 30s after 10 retries)
        _BACKOFF = [2, 4, 8, 16, 30]
        attempt = 0

        while True:
            try:
                async with websockets.connect(HA_WS_URL, max_size=10 * 1024 * 1024) as ws:
                    self.ws = ws
                    attempt = 0  # reset on successful connection
                    await self._authenticate()

                    # Start reader as background task so _send() futures can be resolved
                    reader_task = asyncio.create_task(self._ws_reader())
                    await asyncio.sleep(0)  # yield to event loop so reader_task starts

                    await self._fetch_all_states()
                    await self._fetch_area_entity_map()
                    await self._subscribe_events()
                    if HEARTBEAT_INTERVAL_SECONDS > 0:
                        self._publish_heartbeat_discovery()
                        asyncio.create_task(self._heartbeat_loop())
                    self._schedule_discovery()

                    # Wait for reader forever (exits only on disconnect)
                    await reader_task

            except Exception as exc:
                attempt += 1
                delay = _BACKOFF[min(attempt - 1, len(_BACKOFF) - 1)]
                log.warning(
                    "HA WebSocket disconnected (attempt %d): %s — retrying in %ds",
                    attempt, exc, delay,
                )
                await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(MQTTDownstream().run())