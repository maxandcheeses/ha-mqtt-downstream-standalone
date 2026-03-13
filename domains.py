"""
Domain-specific logic:
  - State formatting for /state topic
  - Attribute sub-topic publishing
  - MQTT discovery payload generation
  - Inbound command routing (MQTT → HA service call)
"""

DOMAIN_MAP = {
    "input_boolean":  "switch",
    "input_number":   "number",
    "input_text":     "text",
    "input_select":   "select",
    "input_datetime": "datetime",
    "input_button":   "button",
}

# Domains that need a different MQTT discovery component type than their HA domain
DISCOVERY_DOMAIN_MAP = {
    "timer": "sensor",
}

def mqtt_domain(entity_id: str) -> str:
    domain = entity_id.split(".")[0]
    return DOMAIN_MAP.get(domain, domain)

def entity_slug(entity_id: str) -> str:
    return entity_id.split(".")[1]

def discovery_domain(entity_id: str) -> str:
    """Return the MQTT discovery component type for an entity (may differ from mqtt_domain)."""
    domain = mqtt_domain(entity_id)
    return DISCOVERY_DOMAIN_MAP.get(domain, domain)

def format_state(state: str, domain: str) -> str:
    if domain == "lock":
        return state.upper()
    if domain in ("switch", "light", "binary_sensor", "fan", "humidifier", "siren"):
        return "ON" if state == "on" else "OFF"
    if domain in ("vacuum", "alarm_control_panel", "media_player"):
        return state.upper()
    if domain in ("cover", "valve"):
        return state.lower()
    return state


# ── Attribute sub-topics ───────────────────────────────────────────────────────

def get_attribute_payloads(domain: str, attrs: dict) -> dict[str, str]:
    """Return {subtopic: payload} for all relevant attribute sub-topics per domain."""
    a = attrs

    if domain == "light":
        result = {}
        brightness = a.get("brightness")
        if brightness is not None:
            result["brightness"] = str(brightness)
        color_temp = a.get("color_temp")
        if color_temp is not None:
            result["color_temp"] = str(color_temp)
        rgb = a.get("rgb_color")
        if rgb:
            result["rgb"] = ",".join(str(v) for v in rgb)
        hs = a.get("hs_color")
        if hs:
            result["hs"] = ",".join(str(v) for v in hs)
        xy = a.get("xy_color")
        if xy:
            result["xy"] = ",".join(str(v) for v in xy)
        effect = a.get("effect")
        if effect:
            result["effect"] = str(effect)
        return result

    if domain == "fan":
        result = {}
        pct = a.get("percentage")
        if pct is not None:
            result["percentage"] = str(pct)
        preset = a.get("preset_mode")
        if preset:
            result["preset_mode"] = str(preset)
        if a.get("oscillating") is not None:
            result["oscillation"] = "oscillate_on" if a.get("oscillating") else "oscillate_off"
        direction = a.get("direction")
        if direction:
            result["direction"] = str(direction)
        return result

    if domain == "climate":
        result = {}
        for key, subtopic in [
            ("temperature",         "temperature"),
            ("current_temperature", "current_temperature"),
            ("target_temp_high",    "target_temp_high"),
            ("target_temp_low",     "target_temp_low"),
            ("hvac_action",         "action"),
            ("fan_mode",            "fan_mode"),
            ("swing_mode",          "swing_mode"),
            ("preset_mode",         "preset_mode"),
        ]:
            val = a.get(key)
            if val is not None:
                result[subtopic] = str(val)
        return result

    if domain == "cover":
        result = {}
        pos = a.get("current_position")
        if pos is not None:
            result["position"] = str(pos)
        tilt = a.get("current_tilt_position")
        if tilt is not None:
            result["tilt"] = str(tilt)
        return result

    if domain == "media_player":
        result = {}
        vol = a.get("volume_level")
        if vol is not None:
            result["volume"] = str(vol)
        muted = a.get("is_volume_muted")
        if muted is not None:
            result["muted"] = "ON" if muted else "OFF"
        source = a.get("source")
        if source:
            result["source"] = str(source)
        sound_mode = a.get("sound_mode")
        if sound_mode:
            result["sound_mode"] = str(sound_mode)
        media_title = a.get("media_title")
        if media_title:
            result["media_title"] = str(media_title)
        media_artist = a.get("media_artist")
        if media_artist:
            result["media_artist"] = str(media_artist)
        return result

    if domain == "humidifier":
        result = {}
        humidity = a.get("humidity")
        if humidity is not None:
            result["target_humidity"] = str(humidity)
        mode = a.get("mode")
        if mode:
            result["mode"] = str(mode)
        current_humidity = a.get("current_humidity")
        if current_humidity is not None:
            result["current_humidity"] = str(current_humidity)
        return result

    if domain == "water_heater":
        result = {}
        temp = a.get("temperature")
        if temp is not None:
            result["temperature"] = str(temp)
        current_temp = a.get("current_temperature")
        if current_temp is not None:
            result["current_temperature"] = str(current_temp)
        mode = a.get("operation_mode")
        if mode:
            result["mode"] = str(mode)
        away = a.get("away_mode")
        if away is not None:
            result["away_mode"] = "ON" if away else "OFF"
        return result

    if domain == "vacuum":
        result = {}
        battery = a.get("battery_level")
        if battery is not None:
            result["battery_level"] = str(battery)
        status = a.get("status")
        if status:
            result["status"] = str(status)
        fan_speed = a.get("fan_speed")
        if fan_speed:
            result["fan_speed"] = str(fan_speed)
        return result

    if domain == "alarm_control_panel":
        result = {}
        code_format = a.get("code_format")
        if code_format:
            result["code_format"] = str(code_format)
        return result

    if domain == "valve":
        result = {}
        pos = a.get("current_position")
        if pos is not None:
            result["position"] = str(pos)
        return result

    if domain == "lawn_mower":
        result = {}
        battery = a.get("battery_level")
        if battery is not None:
            result["battery_level"] = str(battery)
        return result

    if domain == "timer":
        result = {}
        for key in ("duration", "remaining", "finishes_at"):
            val = a.get(key)
            if val is not None:
                result[key] = str(val)
        return result

    return {}


# ── Discovery payloads ─────────────────────────────────────────────────────────

def discovery_payload(entity_id: str, state_obj: dict, mqtt_base: str, discovery_prefix: str = "homeassistant") -> dict | None:
    """Return the MQTT discovery config payload dict, or None if unsupported."""
    domain  = mqtt_domain(entity_id)
    slug    = entity_slug(entity_id)
    attrs   = state_obj.get("attributes", {})
    name    = attrs.get("friendly_name") or slug
    base    = f"{mqtt_base}/{domain}/{slug}"
    dev     = {"identifiers": [f"{mqtt_base}_{slug}"], "name": name}

    common = {
        "name":        None,  # inherit from device name
        "object_id":   slug,  # ensures entity_id is domain.slug without duplication
        "unique_id":   f"{mqtt_base}_{slug}",
        "state_topic": f"{base}/state",
        "device":      dev,
    }

    if domain == "switch":
        return {**common,
            "command_topic": f"{base}/set",
            "payload_on":    "ON",
            "payload_off":   "OFF",
        }

    if domain == "light":
        color_modes = attrs.get("supported_color_modes") or []
        effects     = attrs.get("effect_list") or []
        has_brightness = any(m in color_modes for m in ("brightness", "color_temp", "rgb", "rgbw", "rgbww", "hs", "xy", "white"))
        has_color_temp = "color_temp" in color_modes
        payload = {**common,
            "command_topic": f"{base}/set",
            "payload_on":    "ON",
            "payload_off":   "OFF",
        }
        if has_brightness:
            payload["brightness_state_topic"]   = f"{base}/brightness"
            payload["brightness_command_topic"] = f"{base}/set_brightness"
            payload["brightness_scale"]         = 255
        if has_color_temp:
            payload["color_temp_state_topic"]   = f"{base}/color_temp"
            payload["color_temp_command_topic"] = f"{base}/set_color_temp"
        if "rgb" in color_modes:
            payload["rgb_state_topic"]   = f"{base}/rgb"
            payload["rgb_command_topic"] = f"{base}/set_rgb"
        if "hs" in color_modes:
            payload["hs_state_topic"]   = f"{base}/hs"
            payload["hs_command_topic"] = f"{base}/set_hs"
        if "xy" in color_modes:
            payload["xy_state_topic"]   = f"{base}/xy"
            payload["xy_command_topic"] = f"{base}/set_xy"
        if effects:
            payload["effect_state_topic"]   = f"{base}/effect"
            payload["effect_command_topic"] = f"{base}/set_effect"
            payload["effect_list"]          = effects
        return payload

    if domain == "lock":
        return {**common,
            "command_topic":  f"{base}/set",
            "payload_lock":   "LOCK",
            "payload_unlock": "UNLOCK",
            "state_locked":   "LOCKED",
            "state_unlocked": "UNLOCKED",
        }

    if domain == "cover":
        features = attrs.get("supported_features") or 0
        payload = {**common,
            "command_topic": f"{base}/set",
            "payload_open":  "open",
            "payload_close": "close",
            "payload_stop":  "stop",
            "state_open":    "open",
            "state_closed":  "closed",
            "state_opening": "opening",
            "state_closing": "closing",
        }
        if features & 4:    # SUPPORT_SET_COVER_POSITION
            payload["position_topic"]     = f"{base}/position"
            payload["set_position_topic"] = f"{base}/set_position"
        if features & 128:  # SUPPORT_SET_TILT_POSITION
            payload["tilt_status_topic"]  = f"{base}/tilt"
            payload["tilt_command_topic"] = f"{base}/set_tilt"
        return payload

    if domain == "climate":
        fan_modes    = attrs.get("fan_modes") or []
        swing_modes  = attrs.get("swing_modes") or []
        preset_modes = attrs.get("preset_modes") or []
        payload = {**common,
            "temperature_command_topic":  f"{base}/set_temperature",
            "temperature_state_topic":    f"{base}/temperature",
            "current_temperature_topic":  f"{base}/current_temperature",
            "mode_command_topic":         f"{base}/set_mode",
            "mode_state_topic":           f"{base}/state",
            "action_topic":               f"{base}/action",
            "modes":                      attrs.get("hvac_modes") or [],
            "min_temp":                   attrs.get("min_temp", 7),
            "max_temp":                   attrs.get("max_temp", 35),
            "temp_step":                  attrs.get("target_temp_step", 0.5),
        }
        if fan_modes:
            payload["fan_mode_command_topic"] = f"{base}/set_fan_mode"
            payload["fan_mode_state_topic"]   = f"{base}/fan_mode"
            payload["fan_modes"]              = fan_modes
        if swing_modes:
            payload["swing_mode_command_topic"] = f"{base}/set_swing_mode"
            payload["swing_mode_state_topic"]   = f"{base}/swing_mode"
            payload["swing_modes"]              = swing_modes
        if preset_modes:
            payload["preset_mode_command_topic"] = f"{base}/set_preset_mode"
            payload["preset_mode_state_topic"]   = f"{base}/preset_mode"
            payload["preset_modes"]              = preset_modes
        # Dual setpoint support
        if attrs.get("target_temp_high") is not None:
            payload["temperature_high_command_topic"] = f"{base}/set_target_temp_high"
            payload["temperature_high_state_topic"]   = f"{base}/target_temp_high"
            payload["temperature_low_command_topic"]  = f"{base}/set_target_temp_low"
            payload["temperature_low_state_topic"]    = f"{base}/target_temp_low"
        return payload

    if domain == "fan":
        features     = attrs.get("supported_features") or 0
        preset_modes = attrs.get("preset_modes") or []
        payload = {**common,
            "command_topic": f"{base}/set",
            "payload_on":    "ON",
            "payload_off":   "OFF",
        }
        if features & 1:  # SUPPORT_SET_SPEED
            payload["percentage_state_topic"]   = f"{base}/percentage"
            payload["percentage_command_topic"] = f"{base}/set_percentage"
            payload["speed_range_min"]          = 1
            payload["speed_range_max"]          = 100
        if attrs.get("oscillating") is not None:
            payload["oscillation_state_topic"]   = f"{base}/oscillation"
            payload["oscillation_command_topic"] = f"{base}/set_oscillation"
            payload["payload_oscillation_on"]    = "oscillate_on"
            payload["payload_oscillation_off"]   = "oscillate_off"
        if attrs.get("direction") is not None:
            payload["direction_state_topic"]   = f"{base}/direction"
            payload["direction_command_topic"] = f"{base}/set_direction"
        if preset_modes:
            payload["preset_mode_state_topic"]   = f"{base}/preset_mode"
            payload["preset_mode_command_topic"] = f"{base}/set_preset_mode"
            payload["preset_modes"]              = preset_modes
        return payload

    if domain == "number":
        return {**common,
            "command_topic": f"{base}/set",
            "min":           attrs.get("min") or 0,
            "max":           attrs.get("max") or 100,
            "step":          attrs.get("step") or 1,
            "unit_of_measurement": attrs.get("unit_of_measurement") or "",
            "mode":          attrs.get("mode") or "auto",
        }

    if domain == "select":
        return {**common,
            "command_topic": f"{base}/set",
            "options":       attrs.get("options") or [],
        }

    if domain == "text":
        return {**common,
            "command_topic": f"{base}/set",
            "min":           attrs.get("min") or 0,
            "max":           attrs.get("max") or 255,
            "pattern":       attrs.get("pattern") or "",
            "mode":          attrs.get("mode") or "text",
        }

    if domain == "media_player":
        supported = attrs.get("supported_features") or 0
        payload = {**common,
            "command_topic": f"{base}/set",
            "state_playing":  "PLAYING",
            "state_paused":   "PAUSED",
            "state_stopped":  "STOPPED",
            "state_idle":     "IDLE",
            "state_standby":  "STANDBY",
            "state_on":       "ON",
            "state_off":      "OFF",
        }
        # Volume
        if supported & 4:   # SUPPORT_VOLUME_SET
            payload["volume_state_topic"]   = f"{base}/volume"
            payload["volume_command_topic"] = f"{base}/set_volume"
        if supported & 8:   # SUPPORT_VOLUME_MUTE
            payload["mute_state_topic"]   = f"{base}/muted"
            payload["mute_command_topic"] = f"{base}/set_muted"
        # Source
        source_list = attrs.get("source_list") or []
        if source_list:
            payload["source_state_topic"]  = f"{base}/source"
            payload["source_select_topic"] = f"{base}/set_source"
            payload["source_list"]         = source_list
        # Sound mode
        sound_mode_list = attrs.get("sound_mode_list") or []
        if sound_mode_list:
            payload["sound_mode_state_topic"]   = f"{base}/sound_mode"
            payload["sound_mode_command_topic"] = f"{base}/set_sound_mode"
            payload["sound_mode_list"]          = sound_mode_list
        return payload

    if domain in ("button", "scene", "script"):
        return {**common,
            "command_topic": f"{base}/set",
            "payload_press": "PRESS",
        }

    if domain == "vacuum":
        features       = attrs.get("supported_features") or 0
        fan_speed_list = attrs.get("fan_speed_list") or []
        payload = {**common,
            "command_topic":          f"{base}/set",
            "payload_start":          "start",
            "payload_pause":          "pause",
            "payload_stop":           "stop",
            "payload_return_to_base": "return_to_base",
            "payload_locate":         "locate",
            "payload_clean_spot":     "clean_spot",
            "payload_clean_area":     "clean_area",
            "send_command_topic":     f"{base}/send_command",
        }
        if attrs.get("battery_level") is not None:
            payload["battery_level_topic"] = f"{base}/battery_level"
        if features & 64:   # SUPPORT_STATUS
            payload["charging_topic"] = f"{base}/charging"
            payload["cleaning_topic"] = f"{base}/cleaning"
            payload["docked_topic"]   = f"{base}/docked"
            payload["error_topic"]    = f"{base}/error"
        if fan_speed_list:
            payload["fan_speed_state_topic"]   = f"{base}/fan_speed"
            payload["fan_speed_command_topic"] = f"{base}/set_fan_speed"
            payload["fan_speed_list"]          = fan_speed_list
        return payload

    if domain == "humidifier":
        available_modes = attrs.get("available_modes") or []
        payload = {**common,
            "command_topic":                 f"{base}/set",
            "payload_on":                    "ON",
            "payload_off":                   "OFF",
            "target_humidity_state_topic":   f"{base}/target_humidity",
            "target_humidity_command_topic": f"{base}/set_target_humidity",
            "min_humidity":                  attrs.get("min_humidity", 0),
            "max_humidity":                  attrs.get("max_humidity", 100),
        }
        if attrs.get("current_humidity") is not None:
            payload["current_humidity_topic"] = f"{base}/current_humidity"
        if available_modes:
            payload["mode_state_topic"]   = f"{base}/mode"
            payload["mode_command_topic"] = f"{base}/set_mode"
            payload["modes"]              = available_modes
        return payload

    if domain == "alarm_control_panel":
        return {**common,
            "command_topic":       f"{base}/set",
            "payload_arm_away":    "ARM_AWAY",
            "payload_arm_home":    "ARM_HOME",
            "payload_arm_night":   "ARM_NIGHT",
            "payload_arm_vacation":"ARM_VACATION",
            "payload_arm_custom":  "ARM_CUSTOM_BYPASS",
            "payload_disarm":      "DISARM",
            "payload_trigger":     "TRIGGER",
            "state_armed_away":    "armed_away",
            "state_armed_home":    "armed_home",
            "state_armed_night":   "armed_night",
            "state_armed_vacation":"armed_vacation",
            "state_armed_custom":  "armed_custom_bypass",
            "state_disarmed":      "disarmed",
            "state_triggered":     "triggered",
            "state_arming":        "arming",
            "state_pending":       "pending",
        }

    if domain == "valve":
        features = attrs.get("supported_features") or 0
        payload = {**common,
            "command_topic": f"{base}/set",
            "payload_open":  "open",
            "payload_close": "close",
            "payload_stop":  "stop",
            "state_open":    "open",
            "state_closed":  "closed",
            "state_opening": "opening",
            "state_closing": "closing",
        }
        if features & 4:  # SUPPORT_SET_POSITION
            payload["position_topic"]     = f"{base}/position"
            payload["set_position_topic"] = f"{base}/set_position"
        return payload

    if domain == "water_heater":
        features       = attrs.get("supported_features") or 0
        operation_list = attrs.get("operation_list") or []
        payload = {**common,
            "temperature_state_topic":   f"{base}/temperature",
            "temperature_command_topic": f"{base}/set_temperature",
            "current_temperature_topic": f"{base}/current_temperature",
            "min_temp":                  attrs.get("min_temp", 7),
            "max_temp":                  attrs.get("max_temp", 60),
        }
        if operation_list:
            payload["mode_state_topic"]   = f"{base}/mode"
            payload["mode_command_topic"] = f"{base}/set_operation_mode"
            payload["modes"]              = operation_list
        if features & 2:  # SUPPORT_AWAY_MODE
            payload["away_mode_state_topic"]   = f"{base}/away_mode"
            payload["away_mode_command_topic"] = f"{base}/set_away_mode"
        return payload

    if domain == "siren":
        return {**common,
            "command_topic": f"{base}/set",
            "payload_on":    "ON",
            "payload_off":   "OFF",
            "state_on":      "ON",
            "state_off":     "OFF",
            "available_tones": attrs.get("available_tones") or [],
        }

    if domain == "lawn_mower":
        return {**common,
            "command_topic":          f"{base}/set",
            "payload_start_mowing":   "start_mowing",
            "payload_pause":          "pause",
            "payload_dock":           "dock",
        }

    if domain == "remote":
        return {**common,
            "command_topic": f"{base}/send_command",
        }

    if domain == "timer":
        # HA has no native MQTT timer type — represent as a read-only sensor.
        # State values: idle, active, paused
        # Attributes (duration, remaining, finishes_at) published as JSON to attributes topic.
        return {**common,
            "icon":                  "mdi:timer-outline",
            "json_attributes_topic": f"{base}/attributes",
        }

    return None  # unsupported domain


# ── Inbound command routing ────────────────────────────────────────────────────

def resolve_command(topic: str, payload: str, mqtt_base: str) -> dict | None:
    """
    Parse an inbound MQTT command topic and return:
      {"domain": str, "service": str, "entity_id": str, "data": dict}
    or None if unrecognised.
    """
    prefix = mqtt_base + "/"
    if not topic.startswith(prefix):
        return None

    parts = topic[len(prefix):].split("/")
    if len(parts) < 3:
        return None

    mqtt_dom, slug, *cmd_parts = parts
    command = "/".join(cmd_parts)
    entity_id = f"{mqtt_dom}.{slug}"
    p = payload.strip()

    routes = {
        # Generic on/off/command
        "set": lambda: _route_set(mqtt_dom, entity_id, p),
        # Light
        "set_brightness":   lambda: ("light", "turn_on",  entity_id, {"brightness": int(p)}),
        "set_color_temp":   lambda: ("light", "turn_on",  entity_id, {"color_temp": int(p)}),
        "set_rgb":          lambda: ("light", "turn_on",  entity_id, {"rgb_color":  [int(x) for x in p.split(",")]}),
        "set_hs":           lambda: ("light", "turn_on",  entity_id, {"hs_color":   [float(x) for x in p.split(",")]}),
        "set_xy":           lambda: ("light", "turn_on",  entity_id, {"xy_color":   [float(x) for x in p.split(",")]}),
        "set_effect":       lambda: ("light", "turn_on",  entity_id, {"effect": p}),
        # Fan
        "set_percentage":   lambda: ("fan", "set_percentage", entity_id, {"percentage": int(p)}),
        "set_oscillation":  lambda: ("fan", "oscillate",      entity_id, {"oscillating": p == "oscillate_on"}),
        "set_direction":    lambda: ("fan", "set_direction",  entity_id, {"direction": p}),
        # Shared
        "set_preset_mode":  lambda: (mqtt_dom, "set_preset_mode", entity_id, {"preset_mode": p}),
        # Climate
        "set_temperature":       lambda: (mqtt_dom, "set_temperature",  entity_id, {"temperature": float(p)}),
        "set_target_temp_high":  lambda: ("climate", "set_temperature", entity_id, {"target_temp_high": float(p)}),
        "set_target_temp_low":   lambda: ("climate", "set_temperature", entity_id, {"target_temp_low": float(p)}),
        "set_mode":              lambda: ("climate", "set_hvac_mode",   entity_id, {"hvac_mode": p}),
        "set_fan_mode":          lambda: ("climate", "set_fan_mode",    entity_id, {"fan_mode": p}),
        "set_swing_mode":        lambda: ("climate", "set_swing_mode",  entity_id, {"swing_mode": p}),
        # Cover
        "set_position": lambda: ("cover", "set_cover_position",      entity_id, {"position": int(p)}),
        "set_tilt":     lambda: ("cover", "set_cover_tilt_position", entity_id, {"tilt_position": int(p)}),
        # Valve
        "set_position": lambda: ("valve", "set_valve_position",      entity_id, {"position": int(p)}),
        # Media player
        "set_volume":     lambda: ("media_player", "volume_set",        entity_id, {"volume_level": float(p)}),
        "set_muted":      lambda: ("media_player", "volume_mute",       entity_id, {"is_volume_muted": p == "ON"}),
        "set_source":     lambda: ("media_player", "select_source",     entity_id, {"source": p}),
        "set_sound_mode": lambda: ("media_player", "select_sound_mode", entity_id, {"sound_mode": p}),
        # Vacuum
        "set_fan_speed":   lambda: ("vacuum", "set_fan_speed", entity_id, {"fan_speed": p}),
        "send_command":    lambda: _route_send_command(mqtt_dom, entity_id, p),
        # Humidifier
        "set_target_humidity": lambda: ("humidifier", "set_humidity", entity_id, {"humidity": int(p)}),
        # Water heater
        "set_operation_mode": lambda: ("water_heater", "set_operation_mode", entity_id, {"operation_mode": p}),
        "set_away_mode":      lambda: ("water_heater", "set_away_mode",      entity_id, {"away_mode": p == "ON"}),
    }

    handler = routes.get(command)
    if handler is None:
        return None

    result = handler()
    if result is None:
        return None

    domain, service, eid, data = result
    return {"domain": domain, "service": service, "entity_id": eid, "data": data}


def _route_send_command(mqtt_dom: str, entity_id: str, payload: str):
    """Route a send_command payload (plain string or JSON with command+params) to HA."""
    import json as _json
    try:
        data = _json.loads(payload)
        command = data.pop("command", None)
        params  = data or None
    except (ValueError, AttributeError):
        command = payload.strip()
        params  = None
    if not command:
        return None
    svc_data = {"command": command}
    if params:
        svc_data["params"] = params
    return (mqtt_dom, "send_command", entity_id, svc_data)


def _route_set(mqtt_dom: str, entity_id: str, payload: str):
    p = payload.upper()
    if mqtt_dom == "lock":
        svc = "lock" if p == "LOCK" else "unlock"
        return ("lock", svc, entity_id, {})
    if mqtt_dom == "cover":
        svc = {"OPEN": "open_cover", "CLOSE": "close_cover", "STOP": "stop_cover"}.get(p)
        return ("cover", svc, entity_id, {}) if svc else None
    if mqtt_dom == "valve":
        svc = {"OPEN": "open_valve", "CLOSE": "close_valve", "STOP": "stop_valve"}.get(p)
        return ("valve", svc, entity_id, {}) if svc else None
    if mqtt_dom in ("button", "input_button"):
        return ("button", "press", entity_id, {})
    if mqtt_dom in ("scene", "script"):
        return (mqtt_dom, "turn_on", entity_id, {})
    if mqtt_dom == "climate":
        return ("climate", "set_hvac_mode", entity_id, {"hvac_mode": payload})
    if mqtt_dom == "alarm_control_panel":
        svc = {
            "ARM_AWAY":           "alarm_arm_away",
            "ARM_HOME":           "alarm_arm_home",
            "ARM_NIGHT":          "alarm_arm_night",
            "ARM_VACATION":       "alarm_arm_vacation",
            "ARM_CUSTOM_BYPASS":  "alarm_arm_custom_bypass",
            "DISARM":             "alarm_disarm",
            "TRIGGER":            "alarm_trigger",
        }.get(p)
        return ("alarm_control_panel", svc, entity_id, {}) if svc else None
    if mqtt_dom == "vacuum":
        svc = {
            "START":          "start",
            "PAUSE":          "pause",
            "STOP":           "stop",
            "RETURN_TO_BASE": "return_to_base",
            "LOCATE":         "locate",
            "CLEAN_SPOT":     "clean_spot",
            "CLEAN_AREA":     "clean_area",
        }.get(p)
        return ("vacuum", svc, entity_id, {}) if svc else None
    if mqtt_dom == "lawn_mower":
        svc = {
            "START_MOWING": "start_mowing",
            "PAUSE":        "pause",
            "DOCK":         "dock",
        }.get(p)
        return ("lawn_mower", svc, entity_id, {}) if svc else None
    if mqtt_dom == "media_player":
        svc = {
            "PLAY":    "media_play",
            "PAUSE":   "media_pause",
            "STOP":    "media_stop",
            "NEXT":    "media_next_track",
            "PREV":    "media_previous_track",
            "ON":      "turn_on",
            "OFF":     "turn_off",
        }.get(p)
        return ("media_player", svc, entity_id, {}) if svc else None
    # Generic on/off
    svc = "turn_on" if p == "ON" else "turn_off"
    return (mqtt_dom, svc, entity_id, {})