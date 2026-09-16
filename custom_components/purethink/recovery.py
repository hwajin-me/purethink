"""Recover stalled MQTT network loops without competing with normal reconnects."""
import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
CHECK_INTERVAL = 60


class ConnectionRecovery:
    """Let Paho retry normally, then reload after two disconnected checks."""

    def __init__(self, hass, entry, client):
        self.hass = hass
        self.entry_id = entry.entry_id
        self.client = client
        self.misses = 0
        self.cancel_timer = None
        self.stopped = False
        self.remove_stop_listener = hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self.stop)
        self._schedule()

    def _schedule(self):
        self.cancel_timer = async_call_later(self.hass, CHECK_INTERVAL, self._check)

    @callback
    def _check(self, now):
        self.cancel_timer = None
        data = self.hass.data.get(DOMAIN, {}).get(self.entry_id)
        if self.stopped or data is None or data.get('mqtt') is not self.client:
            self.stop()
            return
        # Paho 1.6 and 2.x can retain a connected flag after the worker exits.
        # There is no public worker-health API; both supported versions expose
        # this thread reference. Recreate the client rather than reuse a dead loop.
        worker = getattr(self.client, "_thread", None)
        healthy = self.client.is_connected() and worker is not None and worker.is_alive()
        self.misses = 0 if healthy else self.misses + 1
        if self.misses >= 2:
            _LOGGER.warning("MQTT reconnect stalled for %s; rebuilding connection", self.entry_id)
            self.misses = 0
            self.hass.config_entries.async_schedule_reload(self.entry_id)
        self._schedule()

    @callback
    def stop(self, event=None):
        self.stopped = True
        if self.cancel_timer is not None:
            self.cancel_timer()
            self.cancel_timer = None
        if self.remove_stop_listener is not None:
            self.remove_stop_listener()
            self.remove_stop_listener = None
