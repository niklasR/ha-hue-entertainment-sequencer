"""Config flow: bridge discovery → push-link → done."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import CONF_BRIDGE_ID, CONF_BRIDGE_IP, CONF_CLIENTKEY, CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


async def _discover_bridge_ip(hass: HomeAssistant) -> str | None:
    """Try Hue cloud discovery; return first bridge IP or None."""
    def _fetch():
        try:
            with urllib.request.urlopen(
                "https://discovery.meethue.com/", timeout=5
            ) as r:
                bridges = json.loads(r.read())
                if bridges:
                    return bridges[0]["internalipaddress"]
        except Exception:
            return None

    return await hass.async_add_executor_job(_fetch)


async def _push_link(hass: HomeAssistant, bridge_ip: str) -> dict | None:
    """
    Attempt one push-link registration.
    Returns {"username": ..., "clientkey": ...} on success, None if button not pressed.
    Raises on any other error.
    """
    def _fetch():
        url = f"https://{bridge_ip}/api"
        data = json.dumps({"devicetype": "hue_entertainment#ha", "generateclientkey": True}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=5) as r:
            return json.loads(r.read())

    result = await hass.async_add_executor_job(_fetch)
    item = result[0]
    if "success" in item:
        return item["success"]
    if "error" in item and item["error"]["type"] == 101:
        return None  # button not pressed yet
    raise ValueError(item.get("error", {}).get("description", "Unknown error"))


async def _get_bridge_id(hass: HomeAssistant, bridge_ip: str) -> str:
    def _fetch():
        with urllib.request.urlopen(
            f"https://{bridge_ip}/api/config", context=_SSL_CTX, timeout=5
        ) as r:
            return json.loads(r.read()).get("bridgeid", bridge_ip)
    return await hass.async_add_executor_job(_fetch)


class HueEntertainmentConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._bridge_ip: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            self._bridge_ip = user_input[CONF_BRIDGE_IP].strip()
            return await self.async_step_link()

        # Pre-fill with discovered IP
        discovered_ip = await _discover_bridge_ip(self.hass) or ""

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_BRIDGE_IP, default=discovered_ip): str,
            }),
            errors=errors,
            description_placeholders={"bridge_ip": discovered_ip},
        )

    async def async_step_link(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                creds = await _push_link(self.hass, self._bridge_ip)
                if creds is None:
                    errors["base"] = "link_button_not_pressed"
                else:
                    bridge_id = await _get_bridge_id(self.hass, self._bridge_ip)
                    await self.async_set_unique_id(bridge_id.lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Hue Bridge {self._bridge_ip}",
                        data={
                            CONF_BRIDGE_IP: self._bridge_ip,
                            CONF_USERNAME: creds["username"],
                            CONF_CLIENTKEY: creds["clientkey"],
                            CONF_BRIDGE_ID: bridge_id,
                        },
                    )
            except Exception as exc:
                _LOGGER.exception("Push-link failed: %s", exc)
                errors["base"] = "link_failed"

        return self.async_show_form(
            step_id="link",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"bridge_ip": self._bridge_ip},
        )
