"""Number entities for live parameter control of Hue Entertainment effects."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_BRIGHTNESS,
    DEFAULT_CYCLE_SPEED,
    DEFAULT_PULSE_RATE_HZ,
    DEFAULT_STROBE_HZ,
    DOMAIN,
    PARAM_BRIGHTNESS,
    PARAM_COLOR_SPEED,
    PARAM_PULSE_RATE,
    PARAM_STROBE_HZ,
)


@dataclass(frozen=True)
class HueParamDescription(NumberEntityDescription):
    param: str = ""
    default: float = 0.0


PARAM_DESCRIPTORS: tuple[HueParamDescription, ...] = (
    HueParamDescription(
        key="strobe_hz",
        param=PARAM_STROBE_HZ,
        name="Strobe Hz",
        icon="mdi:lightning-bolt",
        native_min_value=1,
        native_max_value=50,
        native_step=0.5,
        native_unit_of_measurement="Hz",
        mode=NumberMode.SLIDER,
        default=DEFAULT_STROBE_HZ,
    ),
    HueParamDescription(
        key="color_speed",
        param=PARAM_COLOR_SPEED,
        name="Color Speed",
        icon="mdi:palette",
        native_min_value=0,
        native_max_value=2,
        native_step=0.05,
        native_unit_of_measurement="rot/s",
        mode=NumberMode.SLIDER,
        default=DEFAULT_CYCLE_SPEED,
    ),
    HueParamDescription(
        key="pulse_rate",
        param=PARAM_PULSE_RATE,
        name="Pulse Rate",
        icon="mdi:waveform",
        native_min_value=0,
        native_max_value=5,
        native_step=0.05,
        native_unit_of_measurement="Hz",
        mode=NumberMode.SLIDER,
        default=DEFAULT_PULSE_RATE_HZ,
    ),
    HueParamDescription(
        key="brightness",
        param=PARAM_BRIGHTNESS,
        name="Brightness",
        icon="mdi:brightness-6",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        default=DEFAULT_BRIGHTNESS * 100,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bridge_data = hass.data[DOMAIN][entry.entry_id]
    groups: dict = bridge_data["groups"]

    entities = [
        HueEntertainmentParam(
            entry_id=entry.entry_id,
            group_id=gid,
            group_name=info["name"],
            description=desc,
            hass_data=bridge_data,
        )
        for gid, info in groups.items()
        for desc in PARAM_DESCRIPTORS
    ]
    async_add_entities(entities)


class HueEntertainmentParam(NumberEntity):
    _attr_should_poll = False

    def __init__(
        self,
        entry_id: str,
        group_id: str,
        group_name: str,
        description: HueParamDescription,
        hass_data: dict,
    ) -> None:
        self.entity_description = description
        self._group_id = group_id
        self._attr_name = f"{group_name} {description.name}"
        self._attr_unique_id = f"hue_entertainment_{entry_id}_{group_id}_{description.key}"
        self._attr_native_value = description.default
        self._hass_data = hass_data
        self._param = description.param

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        # Scale brightness from % to 0-1
        stream_value = value / 100 if self._param == PARAM_BRIGHTNESS else value
        self._push_to_stream(stream_value)
        self.async_write_ha_state()

    def _push_to_stream(self, value: float) -> None:
        for entity in self._hass_data.get("entities", {}).values():
            if entity._group_id == self._group_id:
                entity._stream.update_param(self._param, value)
                break
