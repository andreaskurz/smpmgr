# Simple Management Protocol (SMP) Manager

`smpmgr` is a CLI application for interacting with device firmware over a
**serial (UART or USB)**, **Bluetooth (BLE)**, **UDP**, or
**ISO-TP over CAN (Linux SocketCAN)** connection.  It can be used as a
reference implementation of the [smp](https://github.com/JPHutchins/smp) and
[smpclient](https://github.com/intercreate/smpclient) libraries when developing your own SMP
application.

The SMP specification can be found
[here](https://docs.zephyrproject.org/latest/services/device_mgmt/smp_protocol.html).

## CAN / ISO-TP transport (Linux)

`smpmgr` can talk to a Zephyr SMP server built with
`CONFIG_MCUMGR_TRANSPORT_ISOTP` over CAN using Linux SocketCAN and ISO-TP
(ISO 15765-2).  This transport is **Linux only** and requires the optional
`can` extra, which pulls in [`python-can`](https://pypi.org/project/python-can/)
and [`can-isotp`](https://pypi.org/project/can-isotp/):

```
pip install "smpmgr[can]"      # or: poetry install --extras can
```

Bring up a CAN interface, then select it with `--can`:

```
# Virtual CAN (for testing, e.g. against a native_sim server)
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
smpmgr --can vcan0 os echo hello

# Real CAN hardware
sudo ip link set can0 type can bitrate 250000
sudo ip link set up can0
smpmgr --can can0 image upgrade build/zephyr/zephyr.signed.bin
```

The Zephyr ISO-TP transport uses ISO 15765-2 normal addressing with **two** CAN
identifiers, one for each direction. Each peer sends its data frames and its
flow-control frames on the same identifier. `smpmgr` uses the defaults of the
Zephyr server, with TX and RX swapped for the client:

| `smpmgr` option | Default | Must match server |
|---|---|---|
| `--can-tx-id` (data and flow control, client → server) | `0x80` | `CONFIG_MCUMGR_TRANSPORT_ISOTP_RX_ID` |
| `--can-rx-id` (data and flow control, server → client) | `0x180` | `CONFIG_MCUMGR_TRANSPORT_ISOTP_TX_ID` |

The TX identifier of the client must be equal to the RX identifier of the
server, and the opposite. Both options accept decimal (`128`) or hexadecimal
(`0x80`) values. Extended (29-bit) IDs, CAN FD, and the ISO-TP flow-control
block size / STmin can be tuned with `--can-extended-id`, `--can-fd`,
`--can-blocksize`, and `--can-stmin`.

## Install

You can download a portable executable for Windows, Linux, and macOS from the
[latest releases page](https://github.com/intercreate/smpmgr/releases/latest).

`smpmgr` is also [distributed by PyPI](https://pypi.org/project/smpmgr/).  If you already have a
Python environment setup, then it is **strongly recommended** to install `smpmgr` with
[pipx](https://github.com/pypa/pipx) instead of `pip`.

## Custom SMP Groups

`smpmgr` supports user-provided plugins that implement proprietary SMP groups.
```
smpmgr --plugin-path=plugins --help
```

See [/plugins](/plugins) for more information and examples.

## Development Quickstart

> Assumes that you've already [setup your development environment](#development-environment-setup).

1. activate [envr](https://github.com/JPhutchins/envr), the environment manager for **bash**, **zsh**, and **PS**:
   ```
   . ./envr.ps1
   ```
2. run `poetry install` when pulling in new changes
3. run `lint` after making changes
4. run `test` after making changes
5. run `build` to build a portable executable bundle at `dist/smpmgr-<git tag>`.  Refer to `portably.py` for details.
6. add library dependencies with `poetry`:
   ```
   poetry add <my_new_dependency>
   ```
7. add test or other development dependencies using [poetry groups](https://python-poetry.org/docs/managing-dependencies#dependency-groups):
   ```
   poetry add -G dev <my_dev_dependency>
   ```

## Development Environment Setup

### Install Dependencies

- poetry >= 2: https://python-poetry.org/docs/#installation

### Create the venv

```
poetry install
```

The `venv` should be installed to `.venv`.

### Activate envr

> [envr](https://github.com/JPhutchins/envr) supports **bash**, **zsh**, and **PS** in Linux, MacOS, and Windows.  If you are using an unsupported shell, you can activate the `.venv` environment manually, use `poetry run` and `poetry shell`, and refer to `envr-default` for useful aliases.

```
. ./envr.ps1
```

### Verify Your Setup

To verify the installation, make sure that all of the tests are passing using these envr aliases:

```
lint
test
```

### Enable the githooks

> The pre-commit hook will run the linters but not the unit tests.

```
git config core.hooksPath .githooks
```
