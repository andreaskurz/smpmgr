# Plan: ISO-TP over SocketCAN transport for `smpmgr`

## Goal

Add a client transport to the `smpmgr` Python tooling so it can talk to the new
Zephyr **`CONFIG_MCUMGR_TRANSPORT_ISOTP`** SMP server over CAN, using Linux
SocketCAN + ISO-TP (ISO 15765-2). Target usage:

```bash
smpmgr --can can0 os echo hello
smpmgr --can vcan0 image upgrade build/zephyr/zephyr.signed.bin
```

This mirrors the existing `--port` / `--ble` / `--ip` transports.

---

## Background: what already exists

### smpmgr structure (flat package, Poetry, Typer CLI)

- Transports are selected via global Typer callback options in
  [main.py](../../smpmgr/main.py) → stored in a `TransportDefinition`/`Options`
  dataclass → routed to a concrete transport in
  [common.py](../../smpmgr/common.py) `get_custom_smpclient()`.
- `SMPClient(transport, address, timeout)` accepts **any object implementing the
  `SMPTransport` Protocol** — transports are duck-typed, not subclassed.
- `smpclient` is pinned at `==7.0.1` and ships only `serial`, `ble`, `udp`
  transports. **We must not edit the installed package**; instead we add a local
  transport class in smpmgr and pass an instance of it to `SMPClient`.

### The `SMPTransport` Protocol (what we must implement)

From `smpclient/transport/__init__.py`:

```python
class SMPTransport(Protocol):
    _smp_server_transport_buffer_size: int | None = None

    async def connect(self, address: str, timeout_s: float) -> None: ...
    async def disconnect(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    async def receive(self) -> bytes: ...
    async def send_and_receive(self, data: bytes) -> bytes: ...
    def initialize(self, smp_server_transport_buffer_size: int) -> None: ...
    @property
    def mtu(self) -> int: ...
    @property
    def max_unencoded_size(self) -> int: ...
```

`send`/`receive` exchange **complete SMP frames** (8-byte SMP header + CBOR).
The transport is responsible for delivering whole frames; ISO-TP reassembly does
this for us, so `receive()` returns exactly one reassembled SMP message.

### Server-side wire protocol (must match exactly)

From the Zephyr transport `subsys/mgmt/mcumgr/transport/src/smp_isotp.c` +
`Kconfig.isotp`, and the `mcumgr-isotp` sample:

| Parameter | Server value | Kconfig |
|---|---|---|
| Server RX CAN ID | `0x80` | `MCUMGR_TRANSPORT_ISOTP_RX_ID` |
| Server TX CAN ID | `0x180` | `MCUMGR_TRANSPORT_ISOTP_TX_ID` |
| CAN ID type | 11-bit standard (29-bit if `..._EXTENDED_ID=y`) | flags |
| ISO-TP addressing | **Normal** (no extended/fixed addr, no N_AI byte) | flags = 0 |
| Flow-control block size (BS) | `8` | `MCUMGR_TRANSPORT_ISOTP_FC_BS` |
| Flow-control STmin | `0` | `MCUMGR_TRANSPORT_ISOTP_FC_STMIN` |
| TX padding | enabled, byte `0xCC` | `ISOTP_ENABLE_TX_PADDING` |
| RX padding | not required | `ISOTP_REQUIRE_RX_PADDING=n` |
| SMP framing | raw 8B header + CBOR, **no base64, no newline, no length prefix** | — |
| Max SMP message | `1024` (capped at ISO-TP 4095) | `MCUMGR_TRANSPORT_NETBUF_SIZE` |
| CAN FD | off by default; TX DL 64 if `..._CAN_FD=y` | `..._CAN_FD`, `..._TX_DL` |

**Client/server ID swap:** from the client's perspective the IDs are mirrored:

- client **TX** id = server RX = `0x80`
- client **RX** id = server TX = `0x180`

(The `mcumgr-isotp/client/prj.conf` confirms this swap.)

---

## Design decisions

1. **Library:** use [`can-isotp`](https://pypi.org/project/can-isotp/) (imports as
   `isotp`) layered on [`python-can`](https://pypi.org/project/python-can/)'s
   `socketcan` interface. `can-isotp` implements ISO-TP framing, flow control,
   and reassembly in pure Python and matches the Zephyr stack's behavior.

2. **Location:** new module `smpmgr/transport/socketcan_isotp.py` defining
   `SMPSocketCANTransport`. Add `smpmgr/transport/__init__.py` to make it a
   subpackage. Do **not** modify the upstream `smpclient` package.

3. **Async bridge:** `can-isotp`'s `TransportLayer` is thread/blocking-based.
   Run its relay loop in a background thread (`stack.start()` / `stack.stop()` in
   can-isotp v2.x) and bridge the blocking `stack.send()` / `stack.recv()` to
   asyncio via `asyncio.to_thread(...)`. This keeps the transport's public API
   `async`, as the Protocol requires, without an event-loop rewrite.

4. **Address string format:** `--can` takes the SocketCAN interface name
   (`can0`, `vcan0`). CAN IDs, addressing mode, BS/STmin, padding, and CAN-FD
   are exposed as additional options with defaults matching the server, so the
   common case is just `--can can0`.

5. **No SMP-layer encoding:** unlike serial (base64+newline), ISO-TP carries raw
   SMP frames. `send()` passes `data` straight to `stack.send()`; `receive()`
   returns `stack.recv()` verbatim.

6. **MTU:** `mtu` = configured max message (default 1024, cap 4095). Since there
   is no per-frame encoding overhead at the SMP layer, `max_unencoded_size`
   equals `mtu` unless the server advertises a smaller buffer via
   `initialize()`, in which case use that (matching the base-Protocol semantics).

7. **Platform guard:** SocketCAN is Linux-only. Import `isotp`/`can` lazily
   inside the transport module and make the deps an optional extra so non-Linux
   installs of smpmgr keep working. Raise a clear `typer`-friendly error if the
   user passes `--can` without the extra installed or on a non-Linux OS.

---

## Implementation steps

### Step 1 — Dependencies (`pyproject.toml`)

Add an optional `can` extra so the heavyweight CAN deps are opt-in:

```toml
[tool.poetry.dependencies]
python = ">=3.11, <4"
smpclient = { extras = ["all"], version = "==7.0.1" }
typer = { extras = ["all"], version = "==0.24.1" }
readchar = "==4.2.1"
python-can = { version = "^4.3", optional = true }
can-isotp = { version = "^2.0", optional = true }

[tool.poetry.extras]
can = ["python-can", "can-isotp"]
```

(Also add `[project.optional-dependencies]` `can = [...]` if/when the project
migrates to PEP 621 deps; today deps live under `[tool.poetry.dependencies]`.)

Install for development: `poetry install --extras can`.

### Step 2 — New transport module `smpmgr/transport/socketcan_isotp.py`

Implement `SMPSocketCANTransport` against the `SMPTransport` Protocol.

Key elements:

```python
"""SMP transport over ISO-TP (ISO 15765-2) on Linux SocketCAN."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Defaults mirror the Zephyr CONFIG_MCUMGR_TRANSPORT_ISOTP_* server defaults,
# with TX/RX swapped for the client's perspective.
DEFAULT_TX_ID = 0x80   # client TX == server RX (MCUMGR_TRANSPORT_ISOTP_RX_ID)
DEFAULT_RX_ID = 0x180  # client RX == server TX (MCUMGR_TRANSPORT_ISOTP_TX_ID)
DEFAULT_BLOCKSIZE = 8  # MCUMGR_TRANSPORT_ISOTP_FC_BS
DEFAULT_STMIN = 0      # MCUMGR_TRANSPORT_ISOTP_FC_STMIN
DEFAULT_TX_PADDING = 0xCC
DEFAULT_MAX_MSG_SIZE = 1024  # MCUMGR_TRANSPORT_NETBUF_SIZE; ISO-TP hard max 4095


@dataclass
class SocketCANISOTPConfig:
    tx_id: int = DEFAULT_TX_ID
    rx_id: int = DEFAULT_RX_ID
    extended_id: bool = False        # 29-bit if True (ISOTP normal_29bits)
    blocksize: int = DEFAULT_BLOCKSIZE
    stmin: int = DEFAULT_STMIN
    tx_padding: int | None = DEFAULT_TX_PADDING
    can_fd: bool = False
    tx_data_length: int = 8          # 64 when can_fd
    max_msg_size: int = DEFAULT_MAX_MSG_SIZE


class SMPSocketCANTransport:  # structurally implements SMPTransport
    _smp_server_transport_buffer_size: int | None = None

    def __init__(self, config: SocketCANISOTPConfig | None = None) -> None:
        self._config = config or SocketCANISOTPConfig()
        self._bus = None    # can.BusABC
        self._stack = None  # isotp.TransportLayer

    async def connect(self, address: str, timeout_s: float) -> None:
        # Lazy imports so non-CAN installs/platforms are unaffected.
        try:
            import can
            import isotp
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "CAN transport requires the 'can' extra: pip install smpmgr[can]"
            ) from e

        cfg = self._config
        mode = (
            isotp.AddressingMode.Normal_29bits
            if cfg.extended_id
            else isotp.AddressingMode.Normal_11bits
        )
        addr = isotp.Address(mode, txid=cfg.tx_id, rxid=cfg.rx_id)

        # `address` is the SocketCAN channel name, e.g. "can0" / "vcan0".
        self._bus = await asyncio.to_thread(
            can.Bus, channel=address, interface="socketcan", fd=cfg.can_fd
        )

        params = {
            "blocksize": cfg.blocksize,
            "stmin": cfg.stmin,
            "tx_padding": cfg.tx_padding,        # match server 0xCC TX padding
            "can_fd": cfg.can_fd,
            "tx_data_length": cfg.tx_data_length,
            "rx_flowcontrol_timeout": int(timeout_s * 1000),
            "rx_consecutive_frame_timeout": int(timeout_s * 1000),
            # leave RX padding tolerant; server does not require RX padding
        }
        self._stack = isotp.NotifierBasedCanStack(  # or CanStack in older API
            bus=self._bus, address=addr, params=params
        )
        self._stack.start()

    async def disconnect(self) -> None:
        if self._stack is not None:
            await asyncio.to_thread(self._stack.stop)
            self._stack = None
        if self._bus is not None:
            await asyncio.to_thread(self._bus.shutdown)
            self._bus = None

    async def send(self, data: bytes) -> None:
        assert self._stack is not None
        await asyncio.to_thread(self._stack.send, data)
        # send() queues; wait until the ISO-TP TX is drained
        while await asyncio.to_thread(self._stack.transmitting):
            await asyncio.sleep(0.001)

    async def receive(self) -> bytes:
        assert self._stack is not None
        # block in a worker thread until a full reassembled SMP frame arrives
        data = await asyncio.to_thread(self._stack.recv, block=True)
        return bytes(data)

    async def send_and_receive(self, data: bytes) -> bytes:
        await self.send(data)
        return await self.receive()

    def initialize(self, smp_server_transport_buffer_size: int) -> None:
        self._smp_server_transport_buffer_size = smp_server_transport_buffer_size

    @property
    def mtu(self) -> int:
        return min(self._config.max_msg_size, 4095)

    @property
    def max_unencoded_size(self) -> int:
        if self._smp_server_transport_buffer_size is not None:
            return min(self.mtu, self._smp_server_transport_buffer_size)
        return self.mtu
```

> Notes / things to confirm against the installed `can-isotp` version during
> implementation:
> - Exact API surface (`NotifierBasedCanStack` vs `CanStack`, `start/stop` vs
>   `process`, `transmitting()` vs `t_data`/`is_tx_throttled`). v2.x has the
>   thread-based `start()/stop()`; pin `can-isotp ^2.0` to rely on it.
>   On the older v1.x API, run a relay task that calls `stack.process()` in a
>   loop instead.
> - `recv(block=True, timeout=...)`: add a timeout and translate to
>   `asyncio.TimeoutError` so `smp_request()`'s existing timeout handling fires.
> - Whether `Normal_29bits` is the right enum name for extended IDs in the
>   installed version.

### Step 3 — Wire into the CLI

**`smpmgr/common.py`:**

- Add `can: str | None` to `TransportDefinition`.
- Add CAN tuning fields to `Options` (or a nested dataclass), e.g. `can_tx_id`,
  `can_rx_id`, `can_extended_id`, `can_fd`, `can_blocksize`, `can_stmin` — all
  defaulting to the server-matching values so `--can can0` works alone.
- In `get_custom_smpclient()`, add a branch:

```python
elif options.transport.can is not None:
    from smpmgr.transport.socketcan_isotp import (
        SMPSocketCANTransport,
        SocketCANISOTPConfig,
    )
    logger.info(f"Initializing SMPClient with ISO-TP/SocketCAN, {options.transport.can=}")
    config = SocketCANISOTPConfig(
        tx_id=options.can_tx_id,
        rx_id=options.can_rx_id,
        extended_id=options.can_extended_id,
        can_fd=options.can_fd,
        blocksize=options.can_blocksize,
        stmin=options.can_stmin,
        max_msg_size=options.mtu or DEFAULT_MAX_MSG_SIZE,
    )
    return smp_client_cls(
        SMPSocketCANTransport(config), options.transport.can, options.timeout
    )
```

- `connect_with_spinner()` currently catches `SerialException`; add a catch for
  `can.CanError` / `OSError` (e.g. interface down) so CAN connection failures
  produce a clean message rather than a traceback. Import guarded/lazy.

**`smpmgr/main.py`:**

- Add `can: str = typer.Option(None, help="SocketCAN interface for ISO-TP transport, e.g. can0, vcan0")`.
- Add the CAN tuning options (`--can-tx-id`, `--can-rx-id`, `--can-extended-id`,
  `--can-fd`, `--can-blocksize`, `--can-stmin`) with server-matching defaults.
- Pass them into `Options(...)` and `TransportDefinition(..., can=can)`.
- Update the existing `# TODO` note about mutually-exclusive transports to
  include `--can` (optionally add a guard that rejects more than one transport
  selector being set).

### Step 4 — Tests

- `tests/transport/test_socketcan_isotp.py`:
  - Unit-test `SMPSocketCANTransport` with `isotp`/`can` mocked: assert
    `connect()` builds the `isotp.Address` with swapped IDs (tx `0x80`,
    rx `0x180`) and normal 11-bit mode by default; `send()` forwards raw bytes;
    `receive()` returns raw bytes; `mtu`/`max_unencoded_size` math.
  - Assert lazy-import error path raises the helpful message when deps absent.
- Optional integration test (skipped unless `vcan0` is available and
  `SMPMGR_TEST_VCAN=1`): bring up `vcan0`, run a loopback/echo against a
  fixture, gated by `pytest.mark.skipif`.

### Step 5 — Docs & helper

- README: add a "CAN / ISO-TP (Linux)" section documenting `smpmgr[can]` install
  and the `vcan0` bring-up:

  ```bash
  sudo modprobe vcan
  sudo ip link add dev vcan0 type vcan
  sudo ip link set up vcan0
  smpmgr --can vcan0 os echo hello
  ```

  …and the real-hardware case (`ip link set can0 type can bitrate 250000`).
- Note the default CAN IDs and how to override them if the server's
  `MCUMGR_TRANSPORT_ISOTP_RX_ID`/`TX_ID` were changed.

---

## Verification

1. **Static:** `black . && isort . && flake8 && mypy .` (transport must pass the
   project's `disallow_untyped_defs`-style mypy config; the Protocol is
   structural so no explicit base class is needed, but consider an
   `assert isinstance`-free `_: SMPTransport = SMPSocketCANTransport()` smoke
   check in tests to catch signature drift).
2. **Unit:** `pytest tests/transport/test_socketcan_isotp.py`.
3. **End-to-end against the Zephyr server** (per memory: native_sim + vcan0):
   - Bring up `vcan0`; run the `mcumgr-isotp` server `native_sim` build bound to
     `vcan0`.
   - `smpmgr --can vcan0 os echo hello` → expect the echoed string.
   - `smpmgr --can vcan0 image upgrade <signed.bin>` → expect upload progress and
     success; confirm multi-frame ISO-TP transfers reassemble (image > 1 frame
     exercises BS=8/STmin=0 flow control).
   - Sniff with `candump vcan0` to confirm IDs `0x80` (client→server) and `0x180`
     (server→client) and `0xCC` padding.

---

## Risks / open questions

- **`can-isotp` API version drift** — confirm the exact v2.x method names during
  implementation (see notes in Step 2). Pin the version in `pyproject.toml`.
- **Async bridging cost** — `asyncio.to_thread` per send/recv is fine for the
  request/response cadence of SMP; no high-throughput streaming concern.
- **Receive timeout semantics** — ensure `recv()` honors a timeout and surfaces
  `asyncio.TimeoutError` so the existing spinner/timeout UX in `smp_request()`
  works unchanged.
- **CAN FD** — defaults to classic CAN (8-byte frames) to match the server
  default; `--can-fd` + `--can-tx-dl 64` path needs the server built with
  `CONFIG_MCUMGR_TRANSPORT_ISOTP_CAN_FD=y`.
- **Non-Linux** — SocketCAN is Linux-only; `--can` on macOS/Windows should fail
  fast with a clear message.
