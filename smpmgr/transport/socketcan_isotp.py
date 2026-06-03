"""An SMP transport over ISO-TP (ISO 15765-2) on Linux SocketCAN.

This transport talks to a Zephyr SMP server built with
``CONFIG_MCUMGR_TRANSPORT_ISOTP``.  SMP frames (the 8 byte SMP header followed by
the CBOR payload) are carried verbatim as the ISO-TP payload; there is no
base64, newline framing, or length prefix as used by the serial transport.
ISO-TP segmentation, flow control, and reassembly are handled by the
`can-isotp` library on top of `python-can`'s SocketCAN interface, so each
`receive()` returns exactly one reassembled SMP message.

CAN identifiers
---------------
A channel uses ISO 15765-2 normal addressing with two CAN identifiers, one for
each direction. Each peer sends its data frames and its flow-control frames on
the same identifier. The receiver uses the PCI type in the first byte to tell
the frame types apart. The Zephyr transport and `can-isotp` both use this
layout, so the transport gives the frames to the library without changes.
There is no address extension or N_AI byte.

The defaults are the Zephyr ``CONFIG_MCUMGR_TRANSPORT_ISOTP_*`` server
defaults, with TX and RX swapped for the point of view of the client:

================================  =======  ===================================
Parameter                         Default  Zephyr Kconfig (server)
================================  =======  ===================================
client TX id (== server RX id)    0x80     MCUMGR_TRANSPORT_ISOTP_RX_ID
client RX id (== server TX id)    0x180    MCUMGR_TRANSPORT_ISOTP_TX_ID
addressing                        normal   (no extended/fixed addressing)
CAN FD frames                     off      MCUMGR_TRANSPORT_ISOTP_CAN_FD
TX data length (TX_DL, FD only)   64       MCUMGR_TRANSPORT_ISOTP_TX_DL
flow control block size (BS)      8        MCUMGR_TRANSPORT_ISOTP_FC_BS
flow control STmin                0        MCUMGR_TRANSPORT_ISOTP_FC_STMIN
max SMP message size              1024     MCUMGR_TRANSPORT_NETBUF_SIZE
================================  =======  ===================================

``tx_data_length`` (TX_DL) is the number of bytes per CAN FD frame the client
transmits; it only applies when ``can_fd`` is set (classic CAN always uses 8)
and must be one of 8, 12, 16, 20, 24, 32, 48, 64. It governs *upload*
(client -> server) throughput: leaving it at 8 sends 7-byte consecutive frames
even under CAN FD, so raise it (typically 64) to use the larger FD payload.

`python-can` and `can-isotp` are optional dependencies; install them with the
``can`` extra (``pip install smpmgr[can]`` / ``poetry install --extras can``).
SocketCAN is only available on Linux.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Final, Optional

if TYPE_CHECKING:
    import can
    import isotp

logger = logging.getLogger(__name__)

# Defaults mirror the Zephyr CONFIG_MCUMGR_TRANSPORT_ISOTP_* server defaults,
# with the TX/RX CAN IDs swapped for the client's view. Each ID carries the data
# and the flow control of its direction.
DEFAULT_TX_ID: Final = 0x80  # client TX data == server RX (MCUMGR_TRANSPORT_ISOTP_RX_ID)
DEFAULT_RX_ID: Final = 0x180  # client RX data == server TX (MCUMGR_TRANSPORT_ISOTP_TX_ID)
DEFAULT_BLOCKSIZE: Final = 8  # MCUMGR_TRANSPORT_ISOTP_FC_BS
DEFAULT_STMIN: Final = 0  # MCUMGR_TRANSPORT_ISOTP_FC_STMIN
# Max consecutive ISO-TP "Wait" flow-control frames to tolerate from the peer
# before aborting. Must be > 0, otherwise a single Wait (which the Zephyr server
# legitimately sends under RX-buffer pressure, up to its CONFIG_ISOTP_WFTMAX)
# aborts the whole transfer. Mirrors the Zephyr CONFIG_ISOTP_WFTMAX default.
DEFAULT_WFTMAX: Final = 10
DEFAULT_TX_PADDING: Final = 0xCC  # ISO-TP TX frame padding byte
DEFAULT_TX_DL: Final = 64  # MCUMGR_TRANSPORT_ISOTP_TX_DL (CAN FD transmit data length)
DEFAULT_MAX_MSG_SIZE: Final = 1024  # MCUMGR_TRANSPORT_NETBUF_SIZE

# Legal CAN FD frame data lengths (DLC -> byte count). TX_DL must be one of these
# for CAN FD; classic CAN always uses 8.
VALID_TX_DATA_LENGTHS: Final = (8, 12, 16, 20, 24, 32, 48, 64)

# ISO-TP hard limit from the 12 bit length field of a First Frame.
ISOTP_MAX_MSG_SIZE: Final = 4095

# How often to poll the (non-blocking) ISO-TP queues from the event loop.  The
# blocking CAN I/O runs in the `can-isotp` worker threads; these polls only move
# data between those threads and the event loop, so a small interval keeps
# latency low while remaining cleanly cancellable (no leaked worker threads).
_POLL_INTERVAL_S: Final = 0.005


@dataclass(frozen=True)
class SocketCANISOTPConfig:
    """Tunables for `SMPSocketCANTransport`; defaults match the Zephyr server."""

    tx_id: int = DEFAULT_TX_ID  # data and flow control, client -> server
    rx_id: int = DEFAULT_RX_ID  # data and flow control, server -> client
    extended_id: bool = False  # 29 bit CAN IDs (MCUMGR_TRANSPORT_ISOTP_EXTENDED_ID)
    blocksize: int = DEFAULT_BLOCKSIZE
    stmin: int = DEFAULT_STMIN
    wftmax: int = DEFAULT_WFTMAX
    tx_padding: int | None = DEFAULT_TX_PADDING
    can_fd: bool = False  # MCUMGR_TRANSPORT_ISOTP_CAN_FD
    tx_data_length: int = DEFAULT_TX_DL  # CAN(8) or CAN FD(MCUMGR_TRANSPORT_ISOTP_TX_DL)
    max_msg_size: int = DEFAULT_MAX_MSG_SIZE

    def __post_init__(self) -> None:
        # Only meaningful for CAN FD; classic CAN is forced to 8 at send time.
        if self.can_fd and self.tx_data_length not in VALID_TX_DATA_LENGTHS:
            raise ValueError(
                f"tx_data_length must be one of {VALID_TX_DATA_LENGTHS} for CAN FD, "
                f"got {self.tx_data_length}"
            )


def _import_can_isotp() -> tuple[Any, Any]:
    """Import `can` and `isotp` or raise a `RuntimeError` with install hints."""
    if sys.platform != "linux":
        raise RuntimeError(
            "The ISO-TP/SocketCAN transport is only supported on Linux "
            f"(this is {sys.platform})."
        )
    try:
        import can
        import isotp
    except ImportError as e:
        raise RuntimeError(
            "The ISO-TP/SocketCAN transport requires the 'can' extra. "
            "Install it with: pip install smpmgr[can]"
        ) from e
    return can, isotp


class SMPSocketCANTransport:
    """An `smpclient.transport.SMPTransport` for ISO-TP over Linux SocketCAN."""

    _smp_server_transport_buffer_size: int | None = None

    def __init__(self, config: SocketCANISOTPConfig | None = None) -> None:
        self._config: Final = config or SocketCANISOTPConfig()
        self._timeout_s: float = 2.0
        self._bus: can.BusABC | None = None
        self._stack: isotp.TransportLayer | None = None

    def _build_rxfn(self, isotp_mod: Any) -> Callable[[float], Optional["isotp.CanMessage"]]:
        """Read one CAN frame and convert it for `can-isotp`.

        The function ignores error frames and remote frames.
        """
        bus = self._bus

        def rxfn(timeout: float) -> Optional["isotp.CanMessage"]:
            assert bus is not None
            msg = bus.recv(timeout)
            if msg is None or msg.is_error_frame or msg.is_remote_frame:
                return None
            return isotp_mod.CanMessage(
                arbitration_id=msg.arbitration_id,
                data=msg.data,
                extended_id=msg.is_extended_id,
                is_fd=msg.is_fd,
                bitrate_switch=msg.bitrate_switch,
            )

        return rxfn

    def _build_txfn(self, can_mod: Any) -> Callable[["isotp.CanMessage"], None]:
        """Send one CAN frame from `can-isotp` on the bus.

        The library sends data frames and flow-control frames on `tx_id`.
        """
        bus = self._bus

        def txfn(msg: "isotp.CanMessage") -> None:
            assert bus is not None
            bus.send(
                can_mod.Message(
                    arbitration_id=msg.arbitration_id,
                    data=msg.data,
                    is_extended_id=msg.is_extended_id,
                    is_fd=msg.is_fd,
                    bitrate_switch=msg.bitrate_switch,
                )
            )

        return txfn

    async def connect(self, address: str, timeout_s: float) -> None:
        """Open `address` (a SocketCAN interface name, e.g. ``can0``/``vcan0``)."""
        # Bind to local names that don't shadow the TYPE_CHECKING module imports
        # used in annotations below.
        can_mod, isotp_mod = _import_can_isotp()
        cfg = self._config
        self._timeout_s = timeout_s

        addressing_mode = (
            isotp_mod.AddressingMode.Normal_29bits
            if cfg.extended_id
            else isotp_mod.AddressingMode.Normal_11bits
        )
        isotp_address = isotp_mod.Address(addressing_mode, txid=cfg.tx_id, rxid=cfg.rx_id)

        params: dict[str, Any] = {
            "blocksize": cfg.blocksize,
            "stmin": cfg.stmin,
            "wftmax": cfg.wftmax,
            "tx_padding": cfg.tx_padding,
            "can_fd": cfg.can_fd,
            "bitrate_switch": cfg.can_fd,
            "tx_data_length": cfg.tx_data_length if cfg.can_fd else 8,
            "max_frame_size": min(cfg.max_msg_size, ISOTP_MAX_MSG_SIZE),
        }

        # Filter for the one identifier that we receive on: data and flow control
        # of the server arrive on rx_id.
        id_mask = 0x1FFFFFFF if cfg.extended_id else 0x7FF
        can_filters = [{"can_id": cfg.rx_id, "can_mask": id_mask, "extended": cfg.extended_id}]

        logger.info(
            "Opening SocketCAN interface %r for ISO-TP (tx_id=%#x rx_id=%#x, %s)",
            address,
            cfg.tx_id,
            cfg.rx_id,
            "29-bit" if cfg.extended_id else "11-bit",
        )

        def _open() -> tuple[can.BusABC, isotp.TransportLayer]:
            bus = can_mod.Bus(
                channel=address, interface="socketcan", fd=cfg.can_fd, can_filters=can_filters
            )
            self._bus = bus
            stack = isotp_mod.TransportLayer(
                rxfn=self._build_rxfn(isotp_mod),
                txfn=self._build_txfn(can_mod),
                address=isotp_address,
                params=params,
            )
            stack.start()
            return bus, stack

        self._bus, self._stack = await asyncio.to_thread(_open)

    async def disconnect(self) -> None:
        stack, self._stack = self._stack, None
        bus, self._bus = self._bus, None

        def _close() -> None:
            if stack is not None:
                stack.stop()
            if bus is not None:
                bus.shutdown()

        await asyncio.to_thread(_close)

    async def send(self, data: bytes) -> None:
        if self._stack is None:
            raise RuntimeError("send() called before connect()")
        # send() only enqueues into a thread-safe queue and returns immediately;
        # the worker threads do the CAN I/O.  Poll transmitting() until drained.
        self._stack.send(bytes(data))
        while self._stack.transmitting():
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def receive(self) -> bytes:
        if self._stack is None:
            raise RuntimeError("receive() called before connect()")
        # Poll the non-blocking RX queue.  Blocking is left to the caller's
        # asyncio timeout (smpclient wraps request() in `async with timeout()`),
        # which cancels this coroutine cleanly between polls.
        while True:
            frame = self._stack.recv(block=False)
            if frame is not None:
                return bytes(frame)
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def send_and_receive(self, data: bytes) -> bytes:
        await self.send(data)
        return await self.receive()

    def initialize(self, smp_server_transport_buffer_size: int) -> None:
        self._smp_server_transport_buffer_size = smp_server_transport_buffer_size

    @property
    def mtu(self) -> int:
        return min(self._config.max_msg_size, ISOTP_MAX_MSG_SIZE)

    @property
    def max_unencoded_size(self) -> int:
        # ISO-TP carries SMP frames unencoded, so the usable size is the MTU,
        # bounded by the server's advertised buffer size when known.
        if self._smp_server_transport_buffer_size is not None:
            return min(self.mtu, self._smp_server_transport_buffer_size)
        return self.mtu
