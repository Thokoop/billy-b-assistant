"""Advertise the satellite to Home Assistant over mDNS through Avahi.

Home Assistant's ESPHome integration discovers devices through the
``_esphomelib._tcp`` service type and needs a ``mac`` TXT record to offer
adoption. Billy registers that service with the system's avahi-daemon over
D-Bus (``dbus-next`` is already locked for BlueZ), so no extra mDNS stack is
needed. The registration belongs to Billy's D-Bus connection: Avahi removes
it when the connection closes, including after a crash.

Without Avahi, discovery is skipped and Home Assistant can still add Billy by
host name and port.
"""

import asyncio
import contextlib
import re

from ..logger import logger
from .messages import DeviceIdentity


SERVICE_TYPE = '_esphomelib._tcp'

AVAHI_BUS_NAME = 'org.freedesktop.Avahi'
AVAHI_SERVER_INTERFACE = 'org.freedesktop.Avahi.Server'
AVAHI_ENTRY_GROUP_INTERFACE = 'org.freedesktop.Avahi.EntryGroup'
AVAHI_IF_UNSPEC = -1
AVAHI_PROTO_UNSPEC = -1

ENTRY_GROUP_UNCOMMITTED = 0
ENTRY_GROUP_REGISTERING = 1
ENTRY_GROUP_ESTABLISHED = 2
ENTRY_GROUP_COLLISION = 3
ENTRY_GROUP_FAILURE = 4

MAX_COLLISION_RETRIES = 5


class DiscoveryError(Exception):
    """Avahi is unavailable or refused the service registration."""


def mac_txt(mac_address: str) -> str:
    """ESPHome's TXT form of a MAC address: 12 lowercase hex digits."""
    digits = re.sub(r'[^0-9a-fA-F]', '', mac_address).lower()
    if len(digits) != 12:
        raise ValueError(f'not a MAC address: {mac_address!r}')
    return digits


NOISE_PROTOCOL = 'Noise_NNpsk0_25519_ChaChaPoly_SHA256'
ENCRYPTION_NONE = None
ENCRYPTION_SUPPORTED = 'supported'
ENCRYPTION_REQUIRED = 'required'


def txt_records(
    identity: DeviceIdentity, server_info: str, encryption: str | None = None
) -> list[bytes]:
    """TXT records in the shape ESPHome devices publish them.

    ``api_encryption`` tells Home Assistant's discovery to ask for the key;
    ``api_encryption_supported`` that Billy can receive one.
    """
    records = {
        'friendly_name': identity.friendly_name,
        'version': server_info,
        'mac': mac_txt(identity.mac_address),
        'platform': 'Linux',
        'board': identity.model,
        'network': 'wifi',
        'project_name': identity.project_name,
        'project_version': identity.project_version,
    }
    if encryption == ENCRYPTION_REQUIRED:
        records['api_encryption'] = NOISE_PROTOCOL
    elif encryption == ENCRYPTION_SUPPORTED:
        records['api_encryption_supported'] = NOISE_PROTOCOL
    out = []
    for key, value in records.items():
        if not value:
            continue
        # A TXT string holds at most 255 bytes including "key=".
        record = f'{key}={value}'.encode()[:255]
        out.append(record)
    return out


class _Avahi:
    """Thin D-Bus wrapper; a fake with the same methods is used in tests."""

    def __init__(self, bus) -> None:
        self.bus = bus

    async def _call(self, path, interface, member, signature='', body=()):
        from dbus_next import Message, MessageType

        reply = await self.bus.call(
            Message(
                destination=AVAHI_BUS_NAME,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=list(body),
            )
        )
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else ''
            raise DiscoveryError(f'{member}: {reply.error_name} {detail}'.strip())
        return reply.body

    async def entry_group_new(self) -> str:
        return (await self._call('/', AVAHI_SERVER_INTERFACE, 'EntryGroupNew'))[0]

    async def alternative_service_name(self, name: str) -> str:
        body = await self._call(
            '/', AVAHI_SERVER_INTERFACE, 'GetAlternativeServiceName', 's', [name]
        )
        return body[0]

    async def add_service(
        self, group: str, name: str, port: int, txt: list[bytes]
    ) -> None:
        await self._call(
            group,
            AVAHI_ENTRY_GROUP_INTERFACE,
            'AddService',
            'iiussssqaay',
            [
                AVAHI_IF_UNSPEC,
                AVAHI_PROTO_UNSPEC,
                0,
                name,
                SERVICE_TYPE,
                '',  # default domain (.local)
                '',  # this host's Avahi host name
                port,
                txt,
            ],
        )

    async def update_txt(self, group: str, name: str, txt: list[bytes]) -> None:
        await self._call(
            group,
            AVAHI_ENTRY_GROUP_INTERFACE,
            'UpdateServiceTxt',
            'iiusssaay',
            [AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC, 0, name, SERVICE_TYPE, '', txt],
        )

    async def commit(self, group: str) -> None:
        await self._call(group, AVAHI_ENTRY_GROUP_INTERFACE, 'Commit')

    async def reset(self, group: str) -> None:
        await self._call(group, AVAHI_ENTRY_GROUP_INTERFACE, 'Reset')

    async def free(self, group: str) -> None:
        await self._call(group, AVAHI_ENTRY_GROUP_INTERFACE, 'Free')

    async def state(self, group: str) -> int:
        return (await self._call(group, AVAHI_ENTRY_GROUP_INTERFACE, 'GetState'))[0]

    def disconnect(self) -> None:
        self.bus.disconnect()


async def _connect_system_bus():
    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    return _Avahi(await MessageBus(bus_type=BusType.SYSTEM).connect())


class AvahiAdvertiser:
    """Publish and withdraw Billy's ``_esphomelib._tcp`` service."""

    def __init__(
        self,
        identity: DeviceIdentity,
        port: int,
        *,
        server_info: str = 'Billy',
        encryption: str | None = None,
        connect=_connect_system_bus,
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> None:
        self.identity = identity
        self.port = port
        self.server_info = server_info
        self.encryption = encryption
        self.service_name = identity.name
        self._connect = connect
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._avahi: _Avahi | None = None
        self._group: str | None = None

    @property
    def published(self) -> bool:
        return self._group is not None

    async def start(self) -> bool:
        """Publish the service; False (and logged) when Avahi is unavailable."""
        if self._group is not None:
            return True
        try:
            await asyncio.wait_for(self._publish(), self._timeout)
        except (DiscoveryError, OSError, TimeoutError, ValueError) as exc:
            logger.warning(
                f'HA satellite: mDNS discovery unavailable ({exc}); '
                'add Billy in Home Assistant by host name'
            )
            await self.stop()
            return False
        logger.info(
            f'HA satellite: advertising {self.service_name}.{SERVICE_TYPE} '
            f'on port {self.port}'
        )
        return True

    async def _publish(self) -> None:
        txt = txt_records(self.identity, self.server_info, self.encryption)
        self._avahi = await self._connect()
        self._group = await self._avahi.entry_group_new()
        for _ in range(MAX_COLLISION_RETRIES):
            await self._avahi.add_service(
                self._group, self.service_name, self.port, txt
            )
            await self._avahi.commit(self._group)
            state = await self._wait_settled()
            if state == ENTRY_GROUP_ESTABLISHED:
                return
            if state != ENTRY_GROUP_COLLISION:
                raise DiscoveryError(f'Avahi entry group state {state}')
            # Another device on the network uses this name, e.g. a second Billy.
            self.service_name = await self._avahi.alternative_service_name(
                self.service_name
            )
            logger.warning(
                f'HA satellite: mDNS name taken, retrying as {self.service_name}'
            )
            await self._avahi.reset(self._group)
        raise DiscoveryError('no free mDNS service name')

    async def _wait_settled(self) -> int:
        while True:
            state = await self._avahi.state(self._group)
            if state not in (ENTRY_GROUP_UNCOMMITTED, ENTRY_GROUP_REGISTERING):
                return state
            await asyncio.sleep(self._poll_interval)

    async def update_encryption(self, encryption: str | None) -> bool:
        """Republish the TXT record after the encryption state changed."""
        self.encryption = encryption
        avahi, group = self._avahi, self._group
        if avahi is None or group is None:
            return False
        txt = txt_records(self.identity, self.server_info, encryption)
        try:
            await asyncio.wait_for(
                avahi.update_txt(group, self.service_name, txt), self._timeout
            )
        except (DiscoveryError, OSError, TimeoutError) as exc:
            logger.warning(f'HA satellite: could not update mDNS record: {exc}')
            return False
        return True

    async def stop(self) -> None:
        """Withdraw the service; bounded and safe to call repeatedly."""
        avahi, group = self._avahi, self._group
        self._avahi = self._group = None
        if avahi is None:
            return
        if group is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(avahi.free(group), 1.0)
        with contextlib.suppress(Exception):
            avahi.disconnect()
