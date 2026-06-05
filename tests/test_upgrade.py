"""Tests for the `image upgrade` command's mark-for-swap behavior.

These exercise the decision the `upgrade` command makes about whether (and how) to
send an ``ImageStatesWrite`` before resetting the device:

* default, slot 0            -> upload + reset only, no swap armed (upstream behavior)
* ``--test``, slot 0         -> ``ImageStatesWrite(confirm=False)`` (test swap)
* ``--confirm``, slot 0      -> ``ImageStatesWrite(confirm=True)`` (permanent)
* slot != 0 (no flags)       -> ``ImageStatesWrite(confirm=False)`` (upstream behavior)
* ``--test`` + ``--confirm`` -> rejected, nothing sent

The transport/network layer is fully mocked; we only assert on the sequence of SMP
requests the command emits and on the fields of the ``ImageStatesWrite``.
"""

from __future__ import annotations

import types

import pytest
import typer
from smp.message import ResponseType
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite

import smpmgr.main as main
from smpmgr.image_management import ImageFormat

# Hash the *device* reports for a slot (used on the `--format any` path).
DEVICE_HASH = bytes(range(32))
# Hash parsed from the local MCUboot image's SHA256 TLV (the `--format mcuboot` path).
TLV_HASH = bytes(range(31, -1, -1))


class _FakeSuccess:
    """Minimal stand-in for a successful SMP response.

    ``smpclient.generics.success()`` only inspects ``RESPONSE_TYPE``; any extra
    attributes a handler reads (e.g. ``images``) are passed via kwargs.
    """

    RESPONSE_TYPE = ResponseType.SUCCESS

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path):  # type: ignore[no-untyped-def]
    """Patch the upgrade command's network dependencies and record SMP requests.

    Returns a recorder exposing ``requests`` (the ordered list of SMP request objects
    the command emitted), ``file`` (a dummy FW image path), and ``upload_slot`` (the
    slot the test is uploading to, so the mocked ``ImageStatesRead`` can echo it back).
    """
    requests: list[object] = []

    async def fake_smp_request(smpclient, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        requests.append(request)
        if isinstance(request, ImageStatesRead):
            image = types.SimpleNamespace(slot=recorder.upload_slot, hash=DEVICE_HASH)
            return _FakeSuccess(images=[image], splitStatus=None)
        return _FakeSuccess()

    async def fake_async_noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(main, "get_smpclient", lambda options: object())
    monkeypatch.setattr(main, "connect_with_spinner", fake_async_noop)
    monkeypatch.setattr(main, "upload_with_progress_bar", fake_async_noop)
    monkeypatch.setattr(main, "smp_request", fake_smp_request)

    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"\x00" * 64)

    recorder = types.SimpleNamespace(requests=requests, file=fw, upload_slot=0)
    return recorder


def _run(harness, **kwargs):  # type: ignore[no-untyped-def]
    """Invoke `upgrade` with mocked deps, defaulting to `--format any`, slot 0."""
    kwargs.setdefault("file", harness.file)
    kwargs.setdefault("format", ImageFormat.ANY)
    harness.upload_slot = kwargs.get("slot", 0)
    ctx = types.SimpleNamespace(obj=None)
    main.upgrade(ctx, **kwargs)
    return harness.requests


def _only(requests, request_type):  # type: ignore[no-untyped-def]
    """Return the single request of `request_type`, asserting there is exactly one."""
    matches = [r for r in requests if isinstance(r, request_type)]
    assert len(matches) == 1, f"expected exactly one {request_type.__name__}, got {len(matches)}"
    return matches[0]


def test_test_and_confirm_are_mutually_exclusive(harness):  # type: ignore[no-untyped-def]
    ctx = types.SimpleNamespace(obj=None)
    with pytest.raises(typer.Exit) as exc:
        main.upgrade(ctx, file=harness.file, test=True, confirm=True, format=ImageFormat.ANY)
    assert exc.value.exit_code == 1
    # Bails out before connecting/uploading; nothing is sent to the device.
    assert harness.requests == []


def test_default_slot0_does_not_arm_swap(harness):  # type: ignore[no-untyped-def]
    """Upstream default: slot 0 with no flags only uploads and resets."""
    requests = _run(harness)
    assert [type(r) for r in requests] == [ResetWrite]


def test_test_flag_arms_test_swap_on_slot0(harness):  # type: ignore[no-untyped-def]
    requests = _run(harness, test=True)
    # Hash is resolved from the device, image is marked, then the device is reset.
    assert [type(r) for r in requests] == [ImageStatesRead, ImageStatesWrite, ResetWrite]
    sw = _only(requests, ImageStatesWrite)
    assert sw.confirm is False  # test swap, not permanent
    assert sw.hash == DEVICE_HASH


def test_confirm_flag_marks_permanent_on_slot0(harness):  # type: ignore[no-untyped-def]
    requests = _run(harness, confirm=True)
    sw = _only(requests, ImageStatesWrite)
    assert sw.confirm is True
    assert sw.hash == DEVICE_HASH
    # The state-write precedes the reset.
    assert type(requests[-1]) is ResetWrite


def test_slot_nonzero_marks_for_test_by_default(harness):  # type: ignore[no-untyped-def]
    """Upstream behavior for a non-default image is preserved."""
    requests = _run(harness, slot=1)
    sw = _only(requests, ImageStatesWrite)
    assert sw.confirm is False


def test_state_write_precedes_reset(harness):  # type: ignore[no-untyped-def]
    requests = _run(harness, test=True)
    sw_index = next(i for i, r in enumerate(requests) if isinstance(r, ImageStatesWrite))
    reset_index = next(i for i, r in enumerate(requests) if isinstance(r, ResetWrite))
    assert sw_index < reset_index


def test_mcuboot_test_uses_tlv_hash(  # type: ignore[no-untyped-def]
    harness, monkeypatch: pytest.MonkeyPatch
):
    """With `--format mcuboot`, the hash comes from the local TLV, not a device read."""
    fake_info = types.SimpleNamespace(get_tlv=lambda tlv: types.SimpleNamespace(value=TLV_HASH))
    monkeypatch.setattr(
        main, "ImageInfo", types.SimpleNamespace(load_file=lambda path: fake_info)
    )
    requests = _run(harness, test=True, format=ImageFormat.MCUBOOT)
    # No round-trip to the device to discover the hash.
    assert not any(isinstance(r, ImageStatesRead) for r in requests)
    sw = _only(requests, ImageStatesWrite)
    assert sw.hash == TLV_HASH
    assert sw.confirm is False


def test_help_lists_test_and_confirm():  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    result = CliRunner().invoke(main.app, ["upgrade", "--help"])
    assert result.exit_code == 0
    assert "--test" in result.output
    assert "--confirm" in result.output
