import asyncio
import logging
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector

from .const import (
    CONF_MQTT_HOST,
    CONF_MQTT_MODE,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    DOMAIN,
    MQTT_MODE_BRIDGE,
    CONF_BRIDGE_PORT,
    CONF_BRIDGE_CLOUD,
    MQTT_LOCAL_DEFAULT_PORT,
    MQTT_MODE_LOCAL,
    MQTT_MODE_MANUFACTURER,
)

_LOGGER = logging.getLogger(__name__)


class PurethinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Purethink config flow."""

    VERSION = 1

    def __init__(self):
        super().__init__()
        self._pending_config = None
        self._reconfigure_entry = None
        self._original_data = None
        self._original_title = None

    async def async_step_reconfigure(self, user_input=None):
        """Repeat setup for an existing entry without creating another entry."""
        if self._reconfigure_entry is None:
            self._reconfigure_entry = self._get_reconfigure_entry()
            self._original_data = dict(self._reconfigure_entry.data)
            self._original_title = self._reconfigure_entry.title
        return await self.async_step_user(user_input)

    def _check_device_id(self, device_id):
        """The current entry is allowed to keep its own device ID."""
        if self._reconfigure_entry is None:
            self._async_abort_entries_match({"device_id": device_id})
            return
        for entry in self._async_current_entries(include_ignore=False):
            if entry.entry_id != self._reconfigure_entry.entry_id and entry.data.get("device_id") == device_id:
                return "already_configured"
        return None

    def _config_is_current(self):
        entry = self._reconfigure_entry
        return (
            self.hass.config_entries.async_get_entry(entry.entry_id) is entry
            and dict(entry.data) == self._original_data
            and entry.title == self._original_title
        )

    def _registry_migration(self, old_id, new_id):
        """Plan an ID change without mutating either registry."""
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        device = devices.async_get_device(identifiers={(DOMAIN, old_id)})
        target = devices.async_get_device(identifiers={(DOMAIN, new_id)})
        if target is not None and (device is None or target.id != device.id):
            return None
        changes = []
        for entity in er.async_entries_for_config_entry(entities, self._reconfigure_entry.entry_id):
            if entity.platform == DOMAIN and entity.unique_id.startswith(old_id + "_"):
                unique_id = new_id + entity.unique_id[len(old_id):]
                if entities.async_get_entity_id(entity.domain, DOMAIN, unique_id) is not None:
                    return None
                changes.append((entity.entity_id, entity.unique_id, unique_id))
        return device, changes

    async def _finish(self, data):
        """Unload, migrate and commit; recover the old setup on a failed migration."""
        if self._reconfigure_entry is None:
            return self.async_create_entry(title=data["friendly_name"], data=data)
        entry = self._reconfigure_entry
        if not self._config_is_current():
            return self.async_abort(reason="config_changed")
        old_id = entry.data["device_id"]
        new_id = data["device_id"]
        if self._check_device_id(new_id):
            return self.async_abort(reason="already_configured")
        if new_id != old_id and self._registry_migration(old_id, new_id) is None:
            return self.async_abort(reason="already_configured")

        try:
            unloaded = await self.hass.config_entries.async_unload(entry.entry_id)
        except asyncio.CancelledError:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            raise
        except Exception:
            _LOGGER.exception("Could not unload entry for reconfiguration")
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_failed")
        if not unloaded:
            return self.async_abort(reason="reconfigure_failed")

        # Other flows or registry edits may have completed while unload awaited I/O.
        if not self._config_is_current():
            if self.hass.config_entries.async_get_entry(entry.entry_id) is entry:
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="config_changed")
        plan = self._registry_migration(old_id, new_id) if new_id != old_id else (None, [])
        if self._check_device_id(new_id) or plan is None:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        device, changes = plan
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        applied = []
        try:
            for entity_id, previous_id, unique_id in changes:
                entities.async_update_entity(entity_id, new_unique_id=unique_id)
                applied.append((entity_id, previous_id))
            if device is not None:
                devices.async_update_device(device.id, new_identifiers=(
                    device.identifiers - {(DOMAIN, old_id)} | {(DOMAIN, new_id)}
                ))
            return self.async_update_reload_and_abort(
                entry, title=data["friendly_name"], data=data,
            )
        except Exception:
            _LOGGER.exception("Reconfiguration failed; restoring previous configuration")
            for entity_id, previous_id in reversed(applied):
                entities.async_update_entity(entity_id, new_unique_id=previous_id)
            if device is not None:
                devices.async_update_device(device.id, new_identifiers=device.identifiers)
            self.hass.config_entries.async_update_entry(
                entry, title=self._original_title, data=self._original_data,
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_failed")

    async def async_step_user(self, user_input=None):
        """Handle the first setup step."""
        _LOGGER.debug("Starting config flow")
        errors = {}
        existing = dict(self._reconfigure_entry.data) if self._reconfigure_entry else {}
        defaults = {**existing, **(user_input or {})}

        if user_input is not None:
            device_id = user_input.get("device_id", "").strip()
            mqtt_mode = user_input.get(CONF_MQTT_MODE, MQTT_MODE_MANUFACTURER)
            friendly_name = user_input.get("friendly_name", "").strip()

            if len(friendly_name) > 30:
                errors["base"] = "name_too_long"
                _LOGGER.warning("Name too long: %s", friendly_name)
            elif not friendly_name:
                errors["base"] = "name_required"
                _LOGGER.warning("Empty name field")
            elif not device_id or any(c in device_id for c in "/+#"):
                errors["base"] = "invalid_device_id"
            elif mqtt_mode not in (MQTT_MODE_MANUFACTURER, MQTT_MODE_LOCAL, MQTT_MODE_BRIDGE):
                errors["base"] = "invalid_mqtt_mode"
            elif self._check_device_id(device_id):
                errors["base"] = "already_configured"
            else:
                base_id = re.sub(r"[^\w]", "_", friendly_name.lower().replace(" ", "_"))
                entry_data = {
                    **existing,
                    "friendly_name": friendly_name,
                    "device_id": device_id,
                    "base_id": existing.get("base_id", base_id),
                    CONF_MQTT_MODE: mqtt_mode,
                }

                _LOGGER.info("Creating entry with base_id: %s", base_id)

                if mqtt_mode != MQTT_MODE_BRIDGE:
                    entry_data.pop(CONF_BRIDGE_PORT, None)
                    entry_data.pop(CONF_BRIDGE_CLOUD, None)
                if mqtt_mode == MQTT_MODE_BRIDGE:
                    for key in (CONF_MQTT_HOST, CONF_MQTT_PORT, CONF_MQTT_USERNAME, CONF_MQTT_PASSWORD):
                        entry_data.pop(key, None)
                    self._pending_config = entry_data
                    return await self.async_step_bridge()
                if mqtt_mode == MQTT_MODE_LOCAL:
                    self._pending_config = entry_data
                    return await self.async_step_local_mqtt()

                # Manufacturer mode does not use the old local credentials.
                for key in (CONF_MQTT_HOST, CONF_MQTT_PORT, CONF_MQTT_USERNAME, CONF_MQTT_PASSWORD):
                    entry_data.pop(key, None)
                return await self._finish(entry_data)

        return self.async_show_form(
            step_id="reconfigure" if self._reconfigure_entry else "user",
            data_schema=vol.Schema({
                vol.Required(
                    "friendly_name",
                    default=defaults.get("friendly_name", ""),
                    description={"placeholder": "Living room ventilator"},
                ): str,
                vol.Required(
                    "device_id",
                    default=defaults.get("device_id", ""),
                    description={"placeholder": "DIV01-AB1234"},
                ): str,
                vol.Required(
                    CONF_MQTT_MODE,
                    default=defaults.get(CONF_MQTT_MODE, MQTT_MODE_MANUFACTURER),
                ): vol.In({
                    MQTT_MODE_MANUFACTURER: "Manufacturer MQTT",
                    MQTT_MODE_LOCAL: "Local MQTT",
                    MQTT_MODE_BRIDGE: "Embedded TLS bridge",
                }),
            }),
            errors=errors,
        )

    async def async_step_local_mqtt(self, user_input=None):
        """Collect local MQTT connection settings."""
        errors = {}

        if self._pending_config is None:
            return await self.async_step_user()

        defaults = {**self._pending_config, **(user_input or {})}

        if user_input is not None:
            host = user_input.get(CONF_MQTT_HOST, "").strip()
            username = user_input.get(CONF_MQTT_USERNAME, "").strip()
            password = user_input.get(CONF_MQTT_PASSWORD, "")

            try:
                port = int(user_input.get(CONF_MQTT_PORT, MQTT_LOCAL_DEFAULT_PORT))
                if not 1 <= port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                errors["base"] = "invalid_port"

            if not host:
                errors["base"] = "host_required"
            elif self._check_device_id(self._pending_config["device_id"]):
                errors["base"] = "already_configured"
            elif not errors:
                return await self._finish({
                    **self._pending_config,
                    CONF_MQTT_HOST: host,
                    CONF_MQTT_PORT: port,
                    CONF_MQTT_USERNAME: username,
                    CONF_MQTT_PASSWORD: password,
                })

        return self.async_show_form(
            step_id="local_mqtt",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_MQTT_HOST,
                    default=defaults.get(CONF_MQTT_HOST, ""),
                    description={"placeholder": "192.168.0.4"},
                ): str,
                vol.Required(
                    CONF_MQTT_PORT,
                    default=defaults.get(CONF_MQTT_PORT, MQTT_LOCAL_DEFAULT_PORT),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(CONF_MQTT_USERNAME, default=defaults.get(CONF_MQTT_USERNAME, "")): str,
                vol.Optional(CONF_MQTT_PASSWORD, default=defaults.get(CONF_MQTT_PASSWORD, "")):
                    selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
            }),
            errors=errors,
        )


    async def async_step_bridge(self, user_input=None):
        """Configure the embedded device listener and optional cloud relay."""
        if self._pending_config is None:
            return await self.async_step_user()
        errors = {}
        defaults = {**self._pending_config, **(user_input or {})}
        if user_input is not None:
            port = user_input[CONF_BRIDGE_PORT]
            conflict = any(
                entry.entry_id != (self._reconfigure_entry.entry_id if self._reconfigure_entry else None)
                and entry.data.get(CONF_MQTT_MODE) == MQTT_MODE_BRIDGE
                and entry.data.get(CONF_BRIDGE_PORT, 8885) == port
                for entry in self._async_current_entries(include_ignore=False)
            )
            if conflict:
                errors["base"] = "bridge_port_in_use"
            else:
                return await self._finish({**self._pending_config, **user_input})
        return self.async_show_form(
            step_id="bridge",
            data_schema=vol.Schema({
                vol.Required(CONF_BRIDGE_PORT, default=defaults.get(CONF_BRIDGE_PORT, 8885)):
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Required(CONF_BRIDGE_CLOUD, default=defaults.get(CONF_BRIDGE_CLOUD, True)): bool,
            }),
            errors=errors,
        )
