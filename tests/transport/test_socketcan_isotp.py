"""Tests for the ISO-TP over SocketCAN transport.

The unit tests mock the optional `can`/`isotp` dependencies so they run on any
platform without CAN hardware.  An opt-in integration test exercises a real
bidirectional multi-frame ISO-TP exchange over a virtual CAN interface, with a
mirror-configured peer that uses the two identifiers of the Zephyr server. To
enable it, create ``vcan0`` and set ``SMPMGR_TEST_VCAN=1``:

    sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
    SMPMGR_TEST_VCAN=1 pytest tests/transport/test_socketcan_isotp.py
"""

import asyncio
import os
import sys
import types
from enum import Enum
from unittest.mock import MagicMock

import pytest
import typer

from smpmgr.main import parse_can_id
from smpmgr.transport.socketcan_isotp import (
    DEFAULT_RX_ID,
    DEFAULT_TX_ID,
    ISOTP_MAX_MSG_SIZE,
    SMPSocketCANTransport,
    SocketCANISOTPConfig,
    _import_can_isotp,
)


class _AddressingMode(Enum):
    Normal_11bits = 0
    Normal_29bits = 1


def _make_mocks() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return (mock_can, mock_isotp, mock_stack) wired for connect().

    `CanMessage`/`can.Message` are simple recording factories so the rx/tx
    remap functions can be invoked and their arbitration ids inspected.
    """
    mock_stack = MagicMock(name="TransportLayer")
    mock_stack.transmitting.return_value = False

    mock_isotp = MagicMock(name="isotp")
    mock_isotp.AddressingMode = _AddressingMode
    mock_isotp.TransportLayer.return_value = mock_stack
    mock_isotp.CanMessage = lambda **kw: types.SimpleNamespace(**kw)

    mock_can = MagicMock(name="can")
    mock_can.Message = lambda **kw: types.SimpleNamespace(**kw)
    return mock_can, mock_isotp, mock_stack


def _connected(
    monkeypatch: pytest.MonkeyPatch,
    config: SocketCANISOTPConfig | None = None,
    *,
    timeout_s: float = 2.0,
) -> tuple[SMPSocketCANTransport, MagicMock, MagicMock, MagicMock]:
    """Construct and connect a transport with mocked deps; return (transport, mocks)."""
    mock_can, mock_isotp, mock_stack = _make_mocks()
    monkeypatch.setattr(
        "smpmgr.transport.socketcan_isotp._import_can_isotp",
        lambda: (mock_can, mock_isotp),
    )
    transport = SMPSocketCANTransport(config)
    asyncio.run(transport.connect("vcan0", timeout_s))
    return transport, mock_can, mock_isotp, mock_stack


def _rxfn_txfn(mock_isotp: MagicMock):  # type: ignore[no-untyped-def]
    """Extract the rxfn and txfn passed to TransportLayer()."""
    kwargs = mock_isotp.TransportLayer.call_args.kwargs
    return kwargs["rxfn"], kwargs["txfn"]


def test_connect_uses_swapped_default_ids_and_normal_11bit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, mock_can, mock_isotp, _ = _connected(monkeypatch)

    # Client data TX == server RX (0x80), client data RX == server TX (0x180).
    mock_isotp.Address.assert_called_once_with(
        _AddressingMode.Normal_11bits, txid=DEFAULT_TX_ID, rxid=DEFAULT_RX_ID
    )
    # The bus has one filter: data and flow control of the server arrive on rx_id.
    _, kwargs = mock_can.Bus.call_args
    assert kwargs["channel"] == "vcan0"
    assert kwargs["interface"] == "socketcan"
    assert kwargs["fd"] is False
    assert [f["can_id"] for f in kwargs["can_filters"]] == [DEFAULT_RX_ID]
    assert all(f["can_mask"] == 0x7FF for f in kwargs["can_filters"])


def test_connect_extended_id_selects_29bit_and_wide_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    _, mock_can, mock_isotp, _ = _connected(monkeypatch, SocketCANISOTPConfig(extended_id=True))
    assert mock_isotp.Address.call_args.args[0] is _AddressingMode.Normal_29bits
    assert all(f["can_mask"] == 0x1FFFFFFF for f in mock_can.Bus.call_args.kwargs["can_filters"])


def test_connect_passes_flow_control_params(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SocketCANISOTPConfig(blocksize=4, stmin=10, tx_padding=0xCC, wftmax=7)
    _, _, mock_isotp, _ = _connected(monkeypatch, config)
    params = mock_isotp.TransportLayer.call_args.kwargs["params"]
    assert params["blocksize"] == 4
    assert params["stmin"] == 10
    assert params["tx_padding"] == 0xCC
    assert params["can_fd"] is False
    assert params["tx_data_length"] == 8
    assert params["wftmax"] == 7


def test_connect_tolerates_wait_frames_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # wftmax must default > 0, else a single server "Wait" FC aborts the transfer.
    _, _, mock_isotp, _ = _connected(monkeypatch, SocketCANISOTPConfig())
    params = mock_isotp.TransportLayer.call_args.kwargs["params"]
    assert params["wftmax"] > 0


def test_connect_can_fd_sets_tx_data_length_and_brs(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SocketCANISOTPConfig(can_fd=True, tx_data_length=64)
    _, mock_can, mock_isotp, _ = _connected(monkeypatch, config)
    assert mock_can.Bus.call_args.kwargs["fd"] is True
    params = mock_isotp.TransportLayer.call_args.kwargs["params"]
    assert params["can_fd"] is True
    assert params["bitrate_switch"] is True
    assert params["tx_data_length"] == 64


def _tx_frame(data: bytes, arb: int = DEFAULT_TX_ID) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        arbitration_id=arb, data=data, is_extended_id=False, is_fd=False, bitrate_switch=False
    )


def _rx_frame(arb: int, data: bytes = b"\x30\x08\x00") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        arbitration_id=arb,
        data=data,
        is_error_frame=False,
        is_remote_frame=False,
        is_extended_id=False,
        is_fd=False,
        bitrate_switch=False,
    )


def test_txfn_sends_data_and_flow_control_on_tx_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, mock_can, mock_isotp, _ = _connected(monkeypatch)
    bus = mock_can.Bus.return_value
    _, txfn = _rxfn_txfn(mock_isotp)

    txfn(_tx_frame(b"\x02\x01\x02"))  # SingleFrame
    assert bus.send.call_args.args[0].arbitration_id == DEFAULT_TX_ID
    txfn(_tx_frame(b"\x30\x08\x00"))  # FlowControl
    assert bus.send.call_args.args[0].arbitration_id == DEFAULT_TX_ID


def test_rxfn_keeps_arbitration_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _, mock_can, mock_isotp, _ = _connected(monkeypatch)
    bus = mock_can.Bus.return_value
    rxfn, _ = _rxfn_txfn(mock_isotp)

    # Flow control and data on rx_id go to the library unchanged.
    bus.recv.return_value = _rx_frame(DEFAULT_RX_ID)
    assert rxfn(0.01).arbitration_id == DEFAULT_RX_ID
    bus.recv.return_value = _rx_frame(DEFAULT_RX_ID, b"\x21abcdefg")
    assert rxfn(0.01).arbitration_id == DEFAULT_RX_ID


def test_rxfn_drops_error_and_empty_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    _, mock_can, mock_isotp, _ = _connected(monkeypatch)
    bus = mock_can.Bus.return_value
    rxfn, _ = _rxfn_txfn(mock_isotp)

    bus.recv.return_value = None
    assert rxfn(0.01) is None

    bus.recv.return_value = types.SimpleNamespace(
        arbitration_id=DEFAULT_RX_ID, data=b"", is_error_frame=True, is_remote_frame=False
    )
    assert rxfn(0.01) is None


def test_send_forwards_raw_bytes_and_waits_for_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _, _, mock_stack = _connected(monkeypatch)
    mock_stack.transmitting.side_effect = [True, False]
    asyncio.run(transport.send(b"\x00\x01\x02hello"))
    mock_stack.send.assert_called_once_with(b"\x00\x01\x02hello")
    assert mock_stack.transmitting.call_count >= 2


def test_receive_returns_raw_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _, _, mock_stack = _connected(monkeypatch)
    mock_stack.recv.side_effect = [None, bytearray(b"response")]
    result = asyncio.run(transport.receive())
    assert result == b"response"
    assert isinstance(result, bytes)


def test_send_and_receive_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _, _, mock_stack = _connected(monkeypatch)
    mock_stack.recv.return_value = bytearray(b"echo")
    result = asyncio.run(transport.send_and_receive(b"ping"))
    mock_stack.send.assert_called_once_with(b"ping")
    assert result == b"echo"


def test_disconnect_stops_stack_and_shuts_down_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, mock_can, _, mock_stack = _connected(monkeypatch)
    bus = mock_can.Bus.return_value
    asyncio.run(transport.disconnect())
    mock_stack.stop.assert_called_once()
    bus.shutdown.assert_called_once()
    # Idempotent: a second disconnect must not raise or re-call.
    asyncio.run(transport.disconnect())
    mock_stack.stop.assert_called_once()


def test_send_before_connect_raises() -> None:
    transport = SMPSocketCANTransport()
    with pytest.raises(RuntimeError):
        asyncio.run(transport.send(b"x"))


def test_receive_before_connect_raises() -> None:
    transport = SMPSocketCANTransport()
    with pytest.raises(RuntimeError):
        asyncio.run(transport.receive())


def test_default_ids() -> None:
    cfg = SocketCANISOTPConfig()
    # Mirror of the Zephyr server defaults.
    assert (cfg.tx_id, cfg.rx_id) == (0x80, 0x180)


def test_parse_can_id_accepts_hex_and_decimal() -> None:
    assert parse_can_id("0x80") == 0x80
    assert parse_can_id("384") == 384
    assert parse_can_id(0x180) == 0x180
    with pytest.raises(typer.BadParameter):
        parse_can_id("can0")


def test_mtu_and_max_unencoded_size() -> None:
    transport = SMPSocketCANTransport(SocketCANISOTPConfig(max_msg_size=1024))
    assert transport.mtu == 1024
    assert transport.max_unencoded_size == 1024
    transport.initialize(512)
    assert transport.max_unencoded_size == 512
    transport.initialize(4096)
    assert transport.max_unencoded_size == 1024


def test_mtu_capped_at_isotp_limit() -> None:
    transport = SMPSocketCANTransport(SocketCANISOTPConfig(max_msg_size=99999))
    assert transport.mtu == ISOTP_MAX_MSG_SIZE


def test_import_helper_rejects_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="only supported on Linux"):
        _import_can_isotp()


def test_import_helper_reports_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "isotp", None)
    monkeypatch.setitem(sys.modules, "can", None)
    with pytest.raises(RuntimeError, match=r"smpmgr\[can\]"):
        _import_can_isotp()


# --------------------------------------------------------------------------- #
# Opt-in integration test against a real virtual CAN interface.
# --------------------------------------------------------------------------- #

requires_vcan = pytest.mark.skipif(
    os.environ.get("SMPMGR_TEST_VCAN") != "1",
    reason="set SMPMGR_TEST_VCAN=1 and create vcan0 to run the ISO-TP integration test",
)


@requires_vcan
def test_integration_multiframe_round_trip() -> None:
    async def run() -> None:
        client = SMPSocketCANTransport()
        # Mirror-configured peer emulating the Zephyr server.
        server = SMPSocketCANTransport(SocketCANISOTPConfig(tx_id=0x180, rx_id=0x80))
        await client.connect("vcan0", timeout_s=5.0)
        await server.connect("vcan0", timeout_s=5.0)
        try:
            request = bytes(range(256)) * 4  # 1024 B -> multi-frame, multiple FC rounds
            await client.send(request)
            assert await asyncio.wait_for(server.receive(), timeout=5.0) == request

            response = bytes((i * 7) & 0xFF for i in range(900))
            await server.send(response)
            assert await asyncio.wait_for(client.receive(), timeout=5.0) == response
        finally:
            await client.disconnect()
            await server.disconnect()

    asyncio.run(run())
