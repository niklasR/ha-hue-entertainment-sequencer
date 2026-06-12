"""Light entities — one per Hue entertainment group."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ALL_EFFECTS, DOMAIN, EFFECT_SEQUENCE, EFFECT_STATIC, PARAM_SEQUENCE
from .stream import EntertainmentGroupStream, _rgb255_to_16bit

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    groups: dict[str, dict] = hass.data[DOMAIN][entry.entry_id]["groups"]
    bridge_data = hass.data[DOMAIN][entry.entry_id]

    entities = [
        HueEntertainmentGroupLight(
            entry_id=entry.entry_id,
            group_id=gid,
            group_name=info["name"],
            light_ids=info["lights"],
            bridge_ip=bridge_data["bridge_ip"],
            username=bridge_data["username"],
            clientkey=bridge_data["clientkey"],
            hass_data=hass.data[DOMAIN][entry.entry_id],
        )
        for gid, info in groups.items()
    ]
    async_add_entities(entities)


class HueEntertainmentGroupLight(LightEntity):
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_should_poll = False

    def __init__(
        self,
        entry_id: str,
        group_id: str,
        group_name: str,
        light_ids: list[int],
        bridge_ip: str,
        username: str,
        clientkey: str,
        hass_data: dict,
    ) -> None:
        self._group_id = group_id
        self._attr_name = f"{group_name} Entertainment"
        self._attr_unique_id = f"hue_entertainment_{bridge_ip}_{group_id}"
        self._rgb: tuple[int, int, int] = (255, 255, 255)
        self._brightness: int = 255
        self._hass_data = hass_data
        bridge_lights = hass_data.get("bridge_lights", {})
        self._lights_info = [
            {"id": str(lid), "name": bridge_lights.get(str(lid), {}).get("name", f"Light {lid}")}
            for lid in light_ids
        ]
        self._stream = EntertainmentGroupStream(
            bridge_ip=bridge_ip,
            username=username,
            clientkey=clientkey,
            group_id=group_id,
            light_ids=light_ids,
            on_state_change=self._on_stream_state_change,
        )

    # ------------------------------------------------------------------ #
    # State                                                                #
    # ------------------------------------------------------------------ #

    @property
    def effect_list(self) -> list[str]:
        seqs = self.hass.data[DOMAIN].get("sequences", {})
        if seqs:
            return ALL_EFFECTS + sorted(seqs.keys())
        return ALL_EFFECTS

    @property
    def is_on(self) -> bool:
        return self._stream.is_streaming

    @property
    def effect(self) -> str | None:
        e = self._stream.current_effect
        return e if e != EFFECT_STATIC else None

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int]:
        return self._rgb

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    async def async_turn_on(self, **kwargs: Any) -> None:
        effect = kwargs.get(ATTR_EFFECT)
        if ATTR_RGB_COLOR in kwargs:
            self._rgb = kwargs[ATTR_RGB_COLOR]
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]

        # Named sequence effect
        sequences = self.hass.data[DOMAIN].get("sequences", {})
        if effect and effect in sequences:
            self._stream.update_param(PARAM_SEQUENCE, sequences[effect])
            await self._stream.async_start_effect(EFFECT_SEQUENCE)
            return

        color_16 = _rgb255_to_16bit(*self._rgb, self._brightness)
        await self._stream.async_start_effect(
            effect or EFFECT_STATIC,
            color=color_16,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._stream.async_stop()

    # ------------------------------------------------------------------ #

    async def async_added_to_hass(self) -> None:
        # Register in shared data so services can look us up by entity_id
        self._hass_data["entities"][self.entity_id] = self

    def _on_stream_state_change(self) -> None:
        self.schedule_update_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        await self._stream.async_stop()

    # ------------------------------------------------------------------ #
    # Extra state for automation conditions                                #
    # ------------------------------------------------------------------ #

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "group_id": self._group_id,
            "current_effect": self._stream.current_effect,
            "saved_sequences": self.hass.data[DOMAIN].get("sequences", {}),
            "lights": self._lights_info,
        }
