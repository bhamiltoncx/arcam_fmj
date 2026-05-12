"""Test with a fake server"""

import asyncio
import logging
import unittest
import pytest
from datetime import timedelta
from unittest.mock import ANY

from arcam.fmj import (
    CommandCodes,
    CommandNotRecognised,
    ConnectionFailed,
    UnsupportedZone,
)
from arcam.fmj.client import Client, ClientContext
from arcam.fmj.server import Server, ServerContext
from arcam.fmj.state import State

_LOGGER = logging.getLogger(__name__)

# pylint: disable=redefined-outer-name


@pytest.fixture
async def server(request):
    s = Server("localhost", 8888, "AVR450")
    async with ServerContext(s):
        s.register_handler(
            0x01, CommandCodes.POWER, bytes([0xF0]), lambda **kwargs: bytes([0x00])
        )
        s.register_handler(
            0x01, CommandCodes.VOLUME, bytes([0xF0]), lambda **kwargs: bytes([0x01])
        )
        yield s


@pytest.fixture
async def silent_server(request):
    s = Server("localhost", 8888, "AVR450")

    async def process(reader, writer):
        while True:
            if await reader.read(1) == bytes([]):
                break

    s.process_runner = process
    async with ServerContext(s):
        yield s


@pytest.fixture
async def client(request):
    c = Client("localhost", 8888)
    async with ClientContext(c):
        yield c


@pytest.fixture
async def speedy_client(mocker):
    mocker.patch("arcam.fmj.client._HEARTBEAT_INTERVAL", new=timedelta(seconds=1))
    mocker.patch("arcam.fmj.client._HEARTBEAT_TIMEOUT", new=timedelta(seconds=2))
    mocker.patch("arcam.fmj.client._REQUEST_TIMEOUT", new=timedelta(seconds=0.5))
    mocker.patch("arcam.fmj.client._RECONNECT_DELAY_START", new=timedelta(seconds=0.1))


async def test_power(server, client):
    data = await client.request(0x01, CommandCodes.POWER, bytes([0xF0]))
    assert data == bytes([0x00])


async def test_multiple(server, client):
    data = await asyncio.gather(
        client.request(0x01, CommandCodes.POWER, bytes([0xF0])),
        client.request(0x01, CommandCodes.VOLUME, bytes([0xF0])),
    )
    assert data[0] == bytes([0x00])
    assert data[1] == bytes([0x01])


async def test_invalid_command(server, client):
    with pytest.raises(CommandNotRecognised):
        await client.request(0x01, CommandCodes.from_int(0xFF), bytes([0xF0]))


async def test_state(server, client):
    state = State(client, 0x01)
    await state.update()
    assert state.get(CommandCodes.POWER) == bytes([0x00])
    assert state.get(CommandCodes.VOLUME) == bytes([0x01])


async def test_silent_server_request(speedy_client, silent_server, client):
    with pytest.raises(asyncio.TimeoutError):
        await client.request(0x01, CommandCodes.POWER, bytes([0xF0]))


async def test_unsupported_zone(speedy_client, silent_server, client):
    with pytest.raises(UnsupportedZone):
        await client.request(0x02, CommandCodes.DECODE_MODE_STATUS_2CH, bytes([0xF0]))


async def test_reconnect(speedy_client):
    from arcam.fmj.client import _RECONNECT_DELAY_START

    s = Server("localhost", 8888, "AVR450")
    s.register_handler(0x01, CommandCodes.POWER, bytes([0xF0]), lambda **kwargs: bytes([0x00]))
    c = Client("localhost", 8888)

    async with ServerContext(s):
        await c.start()
        process_task = asyncio.create_task(c.process())
        assert await c.request(0x01, CommandCodes.POWER, bytes([0xF0])) == bytes([0x00])

    # Server stopped; restart immediately so the client can reconnect.
    async with ServerContext(s):
        await asyncio.sleep(_RECONNECT_DELAY_START.total_seconds() + 0.5)
        assert not process_task.done(), "process() should still be running after reconnect"
        assert await c.request(0x01, CommandCodes.POWER, bytes([0xF0])) == bytes([0x00])
        process_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await process_task

    await c.stop()


async def test_silent_server_disconnect(speedy_client, silent_server, mocker):
    from arcam.fmj.client import _HEARTBEAT_TIMEOUT

    c = Client("localhost", 8888)
    original_start = c.start
    call_count = 0

    async def fail_on_reconnect():
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise OSError("Server gone")
        await original_start()

    mocker.patch.object(c, "start", side_effect=fail_on_reconnect)

    connected = True
    with pytest.raises(ConnectionFailed):
        async with ClientContext(c):
            await asyncio.sleep(_HEARTBEAT_TIMEOUT.total_seconds() + 1.0)
            connected = c.connected
    assert not connected


async def test_heartbeat(speedy_client, server, client):
    from arcam.fmj.client import _HEARTBEAT_INTERVAL

    with unittest.mock.patch.object(
        server, "process_request", wraps=server.process_request
    ) as req:
        await asyncio.sleep(_HEARTBEAT_INTERVAL.total_seconds() + 0.5)
        req.assert_called_once_with(ANY)


async def test_cancellation(silent_server):
    from arcam.fmj.client import _HEARTBEAT_TIMEOUT

    e = asyncio.Event()
    c = Client("localhost", 8888)

    async def runner():
        await c.start()
        try:
            e.set()
            await c.process()
        finally:
            await c.stop()

    task = asyncio.create_task(runner())
    async with asyncio.timeout(5):
        await e.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
