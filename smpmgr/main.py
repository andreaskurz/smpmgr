"""Entry point for the `smpmgr` application."""

import asyncio
import logging
import sys
from dataclasses import fields
from importlib.metadata import version as get_version
from pathlib import Path
from typing import Final, cast

import typer
import typer.rich_utils
from rich import print
from smp import error as smperr
from smp.os_management import OS_MGMT_RET_RC
from smpclient.generics import error, error_v1, error_v2, success
from smpclient.mcuboot import IMAGE_TLV, ImageInfo, ImageTLVValue, TLVNotFound
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite
from typing_extensions import Annotated, assert_never

from smpmgr import (
    enumeration_management,
    file_management,
    image_management,
    os_management,
    shell_management,
    stat_management,
    terminal,
)
from smpmgr.common import (
    DEFAULT_LINE_BUFFERS,
    DEFAULT_LINE_LENGTH,
    Options,
    TransportDefinition,
    connect_with_spinner,
    get_smpclient,
    smp_request,
)
from smpmgr.image_management import ImageFormat, ImageFormatOption, upload_with_progress_bar
from smpmgr.logging import LogLevel, setup_logging
from smpmgr.plugins import get_plugins
from smpmgr.transport.socketcan_isotp import DEFAULT_BLOCKSIZE as CAN_DEFAULT_BLOCKSIZE
from smpmgr.transport.socketcan_isotp import DEFAULT_RX_ID as CAN_DEFAULT_RX_ID
from smpmgr.transport.socketcan_isotp import DEFAULT_STMIN as CAN_DEFAULT_STMIN
from smpmgr.transport.socketcan_isotp import DEFAULT_TX_DL as CAN_DEFAULT_TX_DL
from smpmgr.transport.socketcan_isotp import DEFAULT_TX_ID as CAN_DEFAULT_TX_ID
from smpmgr.transport.socketcan_isotp import DEFAULT_WFTMAX as CAN_DEFAULT_WFTMAX
from smpmgr.user import intercreate

logger = logging.getLogger(__name__)

# Intercept and modify sys.argv to look for plugins
plugins: Final = get_plugins(sys.argv)

HELP_LINES: Final = (
    f"Simple Management Protocol (SMP) Manager Version {get_version('smpmgr')}\n",
    "\n[dim]Copyright (c) 2023-2025 Intercreate, Inc. and Contributors[/dim]\n",
) + (
    (
        "[bold yellow]"
        "\nNOTE: Plugins have been loaded from the following source(s). Ensure that you "
        "trust the files that have been loaded. The developers and contributors of "
        "this application shall not be held liable for any damages, losses, or other "
        "consequences arising from the use or misuse of this application.\n\n"
        "[/bold yellow]",
        "\n".join(f"  - {plugin.path.resolve()}" for plugin in plugins),
    )
    if plugins
    else ()
)

# Override the dimming of the help text
typer.rich_utils.STYLE_HELPTEXT = ""

app: Final = typer.Typer(help="".join(HELP_LINES), rich_markup_mode="rich")
app.add_typer(os_management.app)
app.add_typer(stat_management.app)
app.add_typer(image_management.app)
app.add_typer(file_management.app)
app.add_typer(enumeration_management.app)
app.add_typer(intercreate.app)
app.command()(shell_management.shell)
app.command()(terminal.terminal)

for plugin in plugins:
    app.add_typer(plugin.app)


@app.callback(invoke_without_command=True)
def options(
    ctx: typer.Context,
    ip: str = typer.Option(None, help="The IP address to connect to for UDP transport"),
    port: str = typer.Option(
        None, help="The serial port to connect to, e.g. COM1, /dev/ttyACM0, etc."
    ),
    ble: str = typer.Option(None, help="The Bluetooth address to connect to"),
    can: str = typer.Option(
        None,
        help="The SocketCAN interface for ISO-TP transport, e.g. can0, vcan0 (Linux only)",
    ),
    timeout: float = typer.Option(
        2.0, help="Transport timeout in seconds; how long to wait for requests"
    ),
    mtu: int
    | None = typer.Option(
        None,
        help=(
            "For UDP transport, the maximum transmission unit."
            " Deprecated for serial transport; use --line-length and --line-buffers instead."
            " Ignored for BLE transport."
        ),
    ),
    baudrate: int
    | None = typer.Option(
        None,
        help=(
            "The baudrate of the serial port to connect to, e.g. 115200."
            " Will default to smpclient upstream value."
        ),
    ),
    line_length: int
    | None = typer.Option(
        None,
        help=(
            "Serial transport line length in bytes."
            " Must match the SMP server configuration."
            f" Default of None will use {DEFAULT_LINE_LENGTH}."
        ),
    ),
    line_buffers: int
    | None = typer.Option(
        None,
        help=(
            "Serial transport line buffer count."
            " Must match the SMP server configuration."
            f" Default of None will use {DEFAULT_LINE_BUFFERS}."
        ),
    ),
    can_tx_id: int = typer.Option(
        CAN_DEFAULT_TX_ID,
        help=(
            "ISO-TP transport TX CAN ID (client -> server)."
            " Must match the server's MCUMGR_TRANSPORT_ISOTP_RX_ID."
        ),
    ),
    can_rx_id: int = typer.Option(
        CAN_DEFAULT_RX_ID,
        help=(
            "ISO-TP transport RX CAN ID (server -> client)."
            " Must match the server's MCUMGR_TRANSPORT_ISOTP_TX_ID."
        ),
    ),
    can_extended_id: bool = typer.Option(
        False,
        help="Use 29-bit (extended) CAN IDs for the ISO-TP transport.",
    ),
    can_fd: bool = typer.Option(
        False,
        help="Use CAN FD frames for the ISO-TP transport.",
    ),
    can_tx_dl: int = typer.Option(
        CAN_DEFAULT_TX_DL,
        help=(
            "ISO-TP CAN FD transmit data length (TX_DL): bytes per frame the client sends."
            " One of 8, 12, 16, 20, 24, 32, 48, 64. Only used with --can-fd; ignored for"
            " classic CAN (always 8). Larger values raise upload throughput."
        ),
    ),
    can_blocksize: int = typer.Option(
        CAN_DEFAULT_BLOCKSIZE,
        help="ISO-TP flow control block size (BS) for the ISO-TP transport.",
    ),
    can_stmin: int = typer.Option(
        CAN_DEFAULT_STMIN,
        help="ISO-TP flow control minimum separation time (STmin) for the ISO-TP transport.",
    ),
    can_wftmax: int = typer.Option(
        CAN_DEFAULT_WFTMAX,
        help=(
            "Max consecutive ISO-TP 'Wait' flow-control frames to tolerate from the server"
            " before aborting. Must be > 0; the server sends these under RX-buffer pressure"
            " (up to its CONFIG_ISOTP_WFTMAX). Set 0 only to reject any wait."
        ),
    ),
    loglevel: LogLevel = typer.Option(None, help="Debug log level"),
    logfile: Path = typer.Option(None, help="Log file path"),
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
    plugin_path: Path = typer.Option(
        None, help="Path to plugin directory. May be used more than once."
    ),
) -> None:
    if plugin_path:
        raise ValueError(
            "--plugin-path should have been removed from sys.argv by the get_plugins(sys.argv) "
            f"call. This is a bug! {sys.argv=}"
        )
    if version:
        print(get_version('smpmgr'))
        raise typer.Exit()

    setup_logging(loglevel, logfile)

    ctx.obj = Options(
        timeout=timeout,
        transport=TransportDefinition(port=port, ble=ble, ip=ip, can=can),
        mtu=mtu,
        baudrate=baudrate,
        line_length=line_length,
        line_buffers=line_buffers,
        can_tx_id=can_tx_id,
        can_rx_id=can_rx_id,
        can_extended_id=can_extended_id,
        can_fd=can_fd,
        can_tx_dl=can_tx_dl,
        can_blocksize=can_blocksize,
        can_stmin=can_stmin,
        can_wftmax=can_wftmax,
    )
    logger.info(ctx.obj)

    if ctx.invoked_subcommand is None:
        if loglevel is not None or logfile is not None:
            raise typer.Exit()
        print("A command is required, see [bold]--help[/bold] for available commands.")
        raise typer.Exit()

    # The transport is inferred from the selector given (--port, --ble, --ip, --can);
    # exactly one must be provided.
    selected = [f.name for f in fields(ctx.obj.transport) if getattr(ctx.obj.transport, f.name)]
    if len(selected) > 1:
        print(
            "Only one transport may be selected, but got: "
            f"[bold]{', '.join('--' + name for name in selected)}[/bold]."
        )
        raise typer.Exit(code=1)


@app.command()
def upgrade(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="Path to FW image")],
    slot: Annotated[int, typer.Option(help="The image slot to upload to")] = 0,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Permanently confirm the image (prevent revert/rollback). "
            "Without this flag (default): the image is marked for test swap, and MCUboot will "
            "revert to the previous image if the new image fails to boot or is not confirmed. "
            "With this flag: the image is immediately marked as permanent, bypassing MCUboot's "
            "test/revert safety mechanism. "
            "[red]WARNING[/red]: Using --confirm skips the test boot and can brick your device. "
            "[bold]Only use this flag in development scenarios where recovery is possible[/bold], "
            "such as: MCUboot has serial recovery enabled and you have access to the physical "
            "serial interface; JTAG/SWD debug headers are exposed and not locked; "
            "or you have another reliable recovery mechanism. "
            "[red]DO NOT[/red] use --confirm in production or field deployments where physical "
            "access for recovery is not available. "
            "Best practice: always test first by marking for test swap, "
            "rebooting to verify the image works, "
            "then confirm the running image with 'smpmgr image state-write --confirm' "
            "(or some other mechanism).",
        ),
    ] = False,
    format: ImageFormatOption = ImageFormat.MCUBOOT,
) -> None:
    """Upload a FW image, mark it for next boot, and reset the device."""

    image_tlv_sha256: ImageTLVValue | None = None

    match format:
        case ImageFormat.MCUBOOT:
            try:
                image_info = ImageInfo.load_file(str(file))
                logger.info(str(image_info))
            except Exception as e:
                typer.echo(
                    f"Inspection of FW image failed: {e}\n"
                    "If this is not an MCUboot image, retry with --format=any."
                )
                raise typer.Exit(code=1)

            try:
                image_tlv_sha256 = image_info.get_tlv(IMAGE_TLV.SHA256)
                logger.info(f"IMAGE_TLV_SHA256: {image_tlv_sha256}")
            except TLVNotFound:
                typer.echo(
                    "Could not find IMAGE_TLV_SHA256 in image. "
                    "If this is not an MCUboot image, retry with --format=any."
                )
                raise typer.Exit(code=1)
        case ImageFormat.ANY:
            pass
        case _ as unreachable:
            assert_never(unreachable)

    options = cast(Options, ctx.obj)
    smpclient = get_smpclient(options)

    async def f() -> None:
        image_hash: bytes | None = None
        await connect_with_spinner(smpclient)

        with open(file, "rb") as f:
            await upload_with_progress_bar(smpclient, f, slot)

        if slot != 0 or confirm:
            match format:
                case ImageFormat.MCUBOOT:
                    assert image_tlv_sha256 is not None
                    image_hash = image_tlv_sha256.value
                case ImageFormat.ANY:
                    r = await smp_request(
                        smpclient, ImageStatesRead(), "Waiting for image states..."
                    )

                    if error(r):
                        print(r)
                        raise typer.Exit(code=1)
                    elif success(r):
                        if len(r.images) == 0:
                            print("No images on device!")
                            raise typer.Exit(code=1)
                        for image in r.images:
                            if image.slot == slot:
                                image_hash = image.hash
                                break
                        if image_hash is None:
                            print(f"Image with slot {slot} not found!")
                            raise typer.Exit(code=1)
                    else:
                        assert_never(r)
                case _ as unreachable:
                    assert_never(unreachable)

            image_states_response = await smp_request(
                smpclient,
                ImageStatesWrite(hash=image_hash, confirm=confirm),
                "Marking uploaded image for permanent upgrade..."
                if confirm
                else "Marking uploaded image for test upgrade...",
            )
            if success(image_states_response):
                pass
            elif error(image_states_response):
                print(image_states_response)
                raise typer.Exit(code=1)
            else:
                assert_never(image_states_response)

        reset_response = await smp_request(smpclient, ResetWrite())
        if success(reset_response):
            pass
        elif error(reset_response):
            if error_v1(reset_response):
                if reset_response.rc != smperr.MGMT_ERR.EOK:
                    print(reset_response)
                    raise typer.Exit(code=1)
            elif error_v2(reset_response):
                if reset_response.err.rc != OS_MGMT_RET_RC.OK:
                    print(reset_response)
                    raise typer.Exit(code=1)
            else:
                assert_never(reset_response)
        else:
            assert_never(reset_response)

        print("Upgrade complete.")

        if slot != 0:
            print("The device may take a few minutes to complete FW swap.")

    asyncio.run(f())


@app.command()
def interactive() -> None:
    """Open the `smpmgr` interactive shell. Type 'exit' or 'quit' to exit."""

    print("".join(HELP_LINES))
    print("Type 'exit' or 'quit' to exit the shell.\n")

    while True:
        args = typer.prompt("smpmgr", prompt_suffix=' >').split()

        if args[0] in {"exit", "quit"}:
            break
        if args[0] == "interactive":
            print("The 'interactive' command cannot be used from within the shell.")
            continue

        try:
            app(args)
        except SystemExit:
            continue
