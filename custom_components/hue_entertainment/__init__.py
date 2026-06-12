"""Hue Entertainment custom integration."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BRIDGE_IP,
    CONF_CLIENTKEY,
    CONF_USERNAME,
    DEFAULT_CYCLE_SPEED,
    DEFAULT_FLASH_COUNT,
    DEFAULT_PULSE_RATE_HZ,
    DEFAULT_STROBE_HZ,
    DOMAIN,
    EFFECT_SEQUENCE,
    EFFECT_STATIC,
    PARAM_SEQUENCE,
    SERVICE_CREATE_AREA,
    SERVICE_DELETE_AREA,
    SERVICE_DELETE_SEQUENCE,
    SERVICE_PLAY_SEQUENCE,
    SERVICE_UPDATE_SEQUENCE,
    SERVICE_SAVE_SEQUENCE,
    SERVICE_START_EFFECT,
    SERVICE_STOP,
    SERVICE_UPDATE_AREA,
    ALL_EFFECTS,
)
from .stream import _rgb255_to_16bit

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.LIGHT, Platform.NUMBER]

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_SERVICE_START_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_ids,
    vol.Optional("effect", default=EFFECT_STATIC): vol.In(ALL_EFFECTS),
    vol.Optional("hz", default=DEFAULT_STROBE_HZ): vol.All(vol.Coerce(float), vol.Range(min=1, max=50)),
    vol.Optional("flash_count", default=DEFAULT_FLASH_COUNT): vol.All(int, vol.Range(min=1, max=20)),
    vol.Optional("pulse_rate", default=DEFAULT_PULSE_RATE_HZ): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=5)),
    vol.Optional("cycle_speed", default=DEFAULT_CYCLE_SPEED): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=5)),
    vol.Optional("rgb_color"): vol.All(list, vol.Length(min=3, max=3)),
    vol.Optional("brightness", default=255): vol.All(int, vol.Range(min=0, max=255)),
})

_SERVICE_STOP_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_ids,
})


async def _fetch_all_lights(hass: HomeAssistant, bridge_ip: str, username: str) -> dict:
    """Return {bridge_id: {name, uniqueid}} for all lights on the bridge."""
    def _fetch():
        url = f"https://{bridge_ip}/api/{username}/lights"
        with urllib.request.urlopen(url, context=_SSL_CTX, timeout=5) as r:
            return {lid: {"name": l["name"], "uniqueid": l.get("uniqueid", "")}
                    for lid, l in json.loads(r.read()).items()}
    return await hass.async_add_executor_job(_fetch)


async def _fetch_entertainment_groups(hass: HomeAssistant, bridge_ip: str, username: str) -> dict:
    """Query bridge for all Entertainment groups. Returns {group_id: {name, lights}}."""
    def _fetch():
        url = f"https://{bridge_ip}/api/{username}/groups"
        with urllib.request.urlopen(url, context=_SSL_CTX, timeout=5) as r:
            all_groups = json.loads(r.read())
        return {
            gid: {"name": g["name"], "lights": [int(lid) for lid in g["lights"]]}
            for gid, g in all_groups.items()
            if g.get("type") == "Entertainment"
        }

    return await hass.async_add_executor_job(_fetch)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    bridge_ip  = entry.data[CONF_BRIDGE_IP]
    username   = entry.data[CONF_USERNAME]
    clientkey  = entry.data[CONF_CLIENTKEY]

    groups, bridge_lights = await asyncio.gather(
        _fetch_entertainment_groups(hass, bridge_ip, username),
        _fetch_all_lights(hass, bridge_ip, username),
    )
    if not groups:
        _LOGGER.warning("No Entertainment groups found on bridge %s", bridge_ip)

    domain_data = hass.data.setdefault(DOMAIN, {})
    if "sequences" not in domain_data:
        store = Store(hass, 1, f"{DOMAIN}.sequences")
        domain_data["_store"] = store
        domain_data["sequences"] = await store.async_load() or {}

    domain_data[entry.entry_id] = {
        "bridge_ip":     bridge_ip,
        "username":      username,
        "clientkey":     clientkey,
        "groups":        groups,
        "bridge_lights": bridge_lights,  # {bridge_id: {name, uniqueid}}
        "entities":      {},
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_START_EFFECT):
        return  # already registered (multiple entries)

    def _all_entities():
        for v in hass.data[DOMAIN].values():
            if isinstance(v, dict) and "entities" in v:
                yield from v["entities"].values()

    async def handle_start_effect(call: ServiceCall) -> None:
        entity_ids = call.data["entity_id"]
        effect     = call.data.get("effect", EFFECT_STATIC)
        hz         = call.data.get("hz", DEFAULT_STROBE_HZ)
        flash_count= call.data.get("flash_count", DEFAULT_FLASH_COUNT)
        pulse_rate = call.data.get("pulse_rate", DEFAULT_PULSE_RATE_HZ)
        cycle_speed= call.data.get("cycle_speed", DEFAULT_CYCLE_SPEED)
        rgb        = call.data.get("rgb_color", [255, 255, 255])
        brightness = call.data.get("brightness", 255)

        color_16 = _rgb255_to_16bit(int(rgb[0]), int(rgb[1]), int(rgb[2]), brightness)

        for entity in _all_entities():
            if entity.entity_id in entity_ids:
                await entity._stream.async_start_effect(
                    effect,
                    color=color_16,
                    hz=hz,
                    flash_count=flash_count,
                    pulse_rate=pulse_rate,
                    cycle_speed=cycle_speed,
                )

    async def handle_stop(call: ServiceCall) -> None:
        entity_ids = call.data["entity_id"]
        for entity in _all_entities():
            if entity.entity_id in entity_ids:
                await entity._stream.async_stop()

    _SEQ_SCHEMA = vol.Schema({
        vol.Required("entity_id"): cv.entity_ids,
        vol.Required("sequence"): dict,
    })

    async def handle_play_sequence(call: ServiceCall) -> None:
        entity_ids = call.data["entity_id"]
        sequence   = call.data["sequence"]
        for entity in _all_entities():
            if entity.entity_id in entity_ids:
                entity._stream.update_param(PARAM_SEQUENCE, sequence)
                await entity._stream.async_start_effect(EFFECT_SEQUENCE)

    async def handle_update_sequence(call: ServiceCall) -> None:
        """Hot-swap sequence data on a running effect without restarting it."""
        entity_ids = call.data["entity_id"]
        sequence   = call.data["sequence"]
        for entity in _all_entities():
            if entity.entity_id in entity_ids:
                entity._stream.update_param(PARAM_SEQUENCE, sequence)

    _SAVE_SEQ_SCHEMA = vol.Schema({
        vol.Required("name"): str,
        vol.Required("sequence"): dict,
    })
    _DELETE_SEQ_SCHEMA = vol.Schema({
        vol.Required("name"): str,
    })

    async def handle_save_sequence(call: ServiceCall) -> None:
        name = call.data["name"]
        hass.data[DOMAIN]["sequences"][name] = call.data["sequence"]
        await hass.data[DOMAIN]["_store"].async_save(hass.data[DOMAIN]["sequences"])
        for entity in _all_entities():
            entity.async_write_ha_state()

    async def handle_delete_sequence(call: ServiceCall) -> None:
        name = call.data["name"]
        hass.data[DOMAIN]["sequences"].pop(name, None)
        await hass.data[DOMAIN]["_store"].async_save(hass.data[DOMAIN]["sequences"])
        for entity in _all_entities():
            entity.async_write_ha_state()

    hass.services.async_register(DOMAIN, SERVICE_START_EFFECT, handle_start_effect, schema=_SERVICE_START_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP, handle_stop, schema=_SERVICE_STOP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_SEQUENCE, handle_play_sequence, schema=_SEQ_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UPDATE_SEQUENCE, handle_update_sequence, schema=_SEQ_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SAVE_SEQUENCE, handle_save_sequence, schema=_SAVE_SEQ_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_SEQUENCE, handle_delete_sequence, schema=_DELETE_SEQ_SCHEMA)
    _register_area_services(hass)


def _resolve_bridge_light_ids(hass: HomeAssistant, entry_data: dict, light_entities: list[str]) -> list[str]:
    """Convert HA entity IDs (light.xyz) to Hue bridge light numbers ("1", "2", …).

    Matching order:
    1. Already a bare digit string → pass through.
    2. Look up entity unique_id in HA registry, match against bridge uniqueid.
    3. Fall back to matching by friendly name.
    """
    bridge_lights = entry_data.get("bridge_lights", {})
    # Build uniqueid → bridge_id lookup (normalise to lowercase, strip trailing -XX endpoint)
    uniqueid_map: dict[str, str] = {}
    for bid, bl in bridge_lights.items():
        uid = bl.get("uniqueid", "").lower()
        if uid:
            uniqueid_map[uid] = bid
            # Also index without trailing endpoint suffix (e.g. "aa:bb:...-0b" → "aa:bb:...")
            base = uid.rsplit("-", 1)[0]
            uniqueid_map.setdefault(base, bid)

    registry = er.async_get(hass)
    result: list[str] = []

    for eid in light_entities:
        if str(eid).lstrip("-").isdigit():
            result.append(str(eid))
            continue

        bridge_id: str | None = None

        # Try entity registry unique_id → bridge uniqueid
        entry = registry.async_get(eid)
        if entry and entry.unique_id:
            uid = entry.unique_id.lower()
            bridge_id = uniqueid_map.get(uid) or uniqueid_map.get(uid.rsplit("-", 1)[0])

        # Fall back to friendly name match
        if not bridge_id:
            state = hass.states.get(eid)
            if state:
                fname = state.attributes.get("friendly_name", "").lower()
                for bid, bl in bridge_lights.items():
                    if bl["name"].lower() == fname:
                        bridge_id = bid
                        break

        if bridge_id:
            result.append(bridge_id)
        else:
            _LOGGER.warning("Could not map entity %s to a bridge light ID — skipping", eid)

    return result


def _register_area_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_CREATE_AREA):
        return

    _AREA_CREATE_SCHEMA = vol.Schema({
        vol.Required("bridge_entry_id"): str,
        vol.Required("name"): str,
        vol.Required("lights"): [str],
        vol.Optional("area_class", default="Free"): str,
    })
    _AREA_UPDATE_SCHEMA = vol.Schema({
        vol.Required("bridge_entry_id"): str,
        vol.Required("area_entity"): cv.entity_id,
        vol.Optional("name"): str,
        vol.Optional("lights"): [str],
    })
    _AREA_DELETE_SCHEMA = vol.Schema({
        vol.Required("bridge_entry_id"): str,
        vol.Required("area_entity"): cv.entity_id,
    })

    def _get_bridge(entry_id: str):
        return hass.data[DOMAIN].get(entry_id)

    async def handle_create_area(call: ServiceCall) -> None:
        data = _get_bridge(call.data["bridge_entry_id"])
        if not data:
            raise ValueError(f"Unknown bridge entry {call.data['bridge_entry_id']}")
        light_ids = _resolve_bridge_light_ids(hass, data, call.data["lights"])
        if not light_ids:
            raise ValueError("No valid lights resolved — check entity IDs")
        result = await _bridge_api(
            hass, data, "POST", "/groups",
            {"type": "Entertainment", "name": call.data["name"],
             "lights": light_ids, "class": call.data.get("area_class", "Free")},
        )
        _LOGGER.info("Created entertainment area: %s", result)

    def _group_id_from_entity(area_entity: str) -> str:
        state = hass.states.get(area_entity)
        if not state:
            raise ValueError(f"Entity {area_entity} not found")
        gid = state.attributes.get("group_id")
        if not gid:
            raise ValueError(f"Entity {area_entity} has no group_id attribute")
        return str(gid)

    async def handle_update_area(call: ServiceCall) -> None:
        data = _get_bridge(call.data["bridge_entry_id"])
        if not data:
            raise ValueError(f"Unknown bridge entry {call.data['bridge_entry_id']}")
        group_id = _group_id_from_entity(call.data["area_entity"])
        body: dict = {}
        if "name" in call.data:
            body["name"] = call.data["name"]
        if "lights" in call.data:
            body["lights"] = _resolve_bridge_light_ids(hass, data, call.data["lights"])
        await _bridge_api(hass, data, "PUT", f"/groups/{group_id}", body)

    async def handle_delete_area(call: ServiceCall) -> None:
        data = _get_bridge(call.data["bridge_entry_id"])
        if not data:
            raise ValueError(f"Unknown bridge entry {call.data['bridge_entry_id']}")
        group_id = _group_id_from_entity(call.data["area_entity"])
        await _bridge_api(hass, data, "DELETE", f"/groups/{group_id}")

    hass.services.async_register(DOMAIN, SERVICE_CREATE_AREA, handle_create_area, schema=_AREA_CREATE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UPDATE_AREA, handle_update_area, schema=_AREA_UPDATE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_AREA, handle_delete_area, schema=_AREA_DELETE_SCHEMA)


async def _bridge_api(hass: HomeAssistant, data: dict, method: str, path: str, body: dict | None = None) -> dict:
    url = f"https://{data['bridge_ip']}/api/{data['username']}{path}"
    payload = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=payload, method=method,
                                  headers={"Content-Type": "application/json"})
    return await hass.async_add_executor_job(
        lambda: json.loads(urllib.request.urlopen(req, context=_SSL_CTX, timeout=5).read())
    )
