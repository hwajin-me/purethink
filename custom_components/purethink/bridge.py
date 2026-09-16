"""Embedded MQTT transport; fan and device packet handling remain in the entities.

Implements the bridge's routing behavior independently using aMQTT. Router DNAT
and firmware preparation remain external prerequisites (see BRIDGE.md).
"""
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from pathlib import Path
import secrets
import ssl
import time

from amqtt.broker import Broker
from amqtt.plugins.base import BaseAuthPlugin, BaseTopicPlugin
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import paho.mqtt.client as mqtt

from .const import CONF_BRIDGE_CLOUD, CONF_BRIDGE_PORT, MQTT_MANUFACTURER_BROKER, MQTT_MANUFACTURER_PORT

_LOGGER = logging.getLogger(__name__)
_BRIDGES = {}


class _BridgeBroker(Broker):
    """Adapt aMQTT 0.11.3's shutdown when a QoS waiter has been cancelled.

    Its broadcast gather can propagate a disconnected client's CancelledError.
    Suppress that child cancellation so shutdown can finish the session monitor
    and listener cleanup, while preserving cancellation of the caller itself.
    """

    async def _shutdown_broadcast_loop(self):
        try:
            await super()._shutdown_broadcast_loop()
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise


class BridgePlugin(BaseAuthPlugin, BaseTopicPlugin):
    """Allow one physical device and password-protected HA clients per listener."""

    @dataclass
    class Config:
        token: str

    @property
    def bridge(self):
        return _BRIDGES[self.config.token]

    async def authenticate(self, *, session):
        return session.client_id == self.bridge.device_id or (
            session.username == "homeassistant" and session.password == self.config.token
        )

    async def topic_filtering(self, *, session=None, topic=None, action=None):
        return (self.bridge.owns_topic(topic) or topic == self.bridge.prefix + "/#") and (
            session.client_id == self.bridge.device_id or
            (session.username == "homeassistant" and session.password == self.config.token)
        )

    async def on_broker_message_broadcast(self, *, client_id, message):
        # This event fires only after the broker's publish ACL has accepted it.
        if client_id == self.bridge.device_id and self.bridge.owns_topic(message.topic):
            self.bridge.to_cloud(message.topic, bytes(message.data))


def _tls_context(directory):
    """Load a stable self-signed device certificate outside the HA event loop."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    keyfile, certfile = directory / "key.pem", directory / "cert.pem"
    if not keyfile.exists() or not certfile.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, MQTT_MANUFACTURER_BROKER)])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(MQTT_MANUFACTURER_BROKER)]), False)
                .sign(key, hashes.SHA256()))
        keyfile.touch(mode=0o600, exist_ok=True)
        keyfile.chmod(0o600)
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    return context


class EmbeddedBridge:
    """A per-entry TLS broker with optional, non-blocking manufacturer relay."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.device_id = entry.data['device_id']
        self.prefix = f"/things/{self.device_id}"
        self.topic = self.prefix + "/shadow"
        self.token = secrets.token_hex(32)
        self.broker = None
        self.server = None
        self.cloud = None
        self.closed = False
        self.echoes = deque(maxlen=512)
        self.tasks = set()
        self.stop_lock = asyncio.Lock()

    async def start(self):
        context = await self.hass.async_add_executor_job(
            _tls_context, self.hass.config.path('.storage', 'purethink_bridge', self.entry.entry_id)
        )
        _BRIDGES[self.token] = self
        self.broker = _BridgeBroker({
            'listeners': {'default': {'type': 'external'}},
            'plugins': {__name__ + '.BridgePlugin': {'token': self.token}},
            'session_expiry_interval': 60,
        })
        await self.broker.start()
        self.server = await asyncio.start_server(
            self._connected, '0.0.0.0', self.entry.data.get(CONF_BRIDGE_PORT, 8885),
            ssl=context, ssl_handshake_timeout=10,
        )
        if self.entry.data.get(CONF_BRIDGE_CLOUD, True):
            job = self.hass.async_add_executor_job(self._start_cloud)
            try:
                await asyncio.shield(job)
            finally:
                # Executor startup must finish before cancellation can tear down the relay.
                await job
        return {'mode': 'bridge', 'host': '127.0.0.1',
                'port': self.server.sockets[0].getsockname()[1],
                'username': 'homeassistant', 'password': self.token, 'tls': True}

    async def _connected(self, reader, writer):
        await self.broker.stream_connected(reader, writer, 'default')

    def _start_cloud(self):
        self.cloud = mqtt.Client(client_id=f"purethink-ha-{self.entry.entry_id}")
        self.cloud.enable_logger(_LOGGER)
        self.cloud.tls_set(cert_reqs=ssl.CERT_NONE)
        self.cloud.tls_insecure_set(True)
        self.cloud.on_connect = self._cloud_connected
        self.cloud.on_message = self._cloud_message
        self.cloud.reconnect_delay_set(min_delay=5, max_delay=60)
        self.cloud.connect_async(MQTT_MANUFACTURER_BROKER, MQTT_MANUFACTURER_PORT, 60)
        self.cloud.loop_start()

    def _cloud_connected(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(self.prefix + '/#')

    def _cloud_message(self, client, userdata, message):
        self.hass.loop.call_soon_threadsafe(self._receive_cloud, message.topic, bytes(message.payload))

    def owns_topic(self, topic):
        return isinstance(topic, str) and topic.startswith(self.prefix + '/') and not any(c in topic for c in '+#')

    def _receive_cloud(self, topic, payload):
        if self.closed or not self.owns_topic(topic):
            return
        now = time.monotonic()
        while self.echoes and self.echoes[0][1] <= now:
            self.echoes.popleft()
        digest = hashlib.sha256(topic.encode() + b'\0' + payload).digest()
        for echo in self.echoes:
            if echo[0] == digest:
                self.echoes.remove(echo)
                return
        task = self.hass.async_create_task(self.broker.internal_message_broadcast(topic, payload, qos=0))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def to_cloud(self, topic, payload):
        if not self.closed and self.cloud and self.cloud.is_connected():
            # Record before publishing so a fast broker echo cannot race delivery.
            self.echoes.append((hashlib.sha256(topic.encode() + b'\0' + payload).digest(), time.monotonic() + 5))
            self.cloud.publish(topic, payload, qos=0, retain=False)

    async def stop(self, event=None):
        async with self.stop_lock:
            if self.closed:
                return
            await self._stop()

    async def _stop(self):
        self.closed = True
        if self.server:
            self.server.close()
            self.server.close_clients()
        if self.cloud:
            for cleanup in (self.cloud.disconnect, self.cloud.loop_stop):
                try:
                    await self.hass.async_add_executor_job(cleanup)
                except Exception:
                    _LOGGER.debug('Cloud relay cleanup failed', exc_info=True)
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.broker and self.broker.transitions.state == 'started':
            await self.broker.shutdown()
        if self.server:
            await self.server.wait_closed()
        _BRIDGES.pop(self.token, None)
