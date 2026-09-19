# hdcutils

**A pure-python HarmonyOS hdc library, modeled after
[openatx/adbutils](https://github.com/openatx/adbutils). Every device
operation speaks the wire protocol directly to the hdc server -- no hdc.exe
involved, and no dependence on its version or install location.**

`hdc` is to HarmonyOS what adb is to Android. `hdcutils` talks to the hdc
server (the resident process on the dev machine, listening on
`127.0.0.1:8710` by default) with plain Python, and drives real
devices/emulators the way adbutils drives Android:

```python
import hdcutils

hdc = hdcutils.HdcClient()
hdc.list_targets()                        # ['2JAV4C1579000596', ...]

d = hdc.device()                          # auto-select when only one device
d.shell("param get const.product.model")  # 'HUAWEI Mate 60 Pro'
d.get_prop("const.ohos.apiversion")       # '12'
d.send_file("app.hap", "/data/local/tmp/app.hap")   # pure-socket file protocol
d.install("app.hap")                       # pure-socket install protocol
d.app_start("com.example.app")             # aa start
d.app_current().package                    # foreground app
d.click(400, 800); d.swipe(400, 1600, 400, 400)   # uitest uiInput
d.screenshot("screen.jpeg")                # snapshot_display -> PIL.Image
for line in d.hilog(timeout=10):           # streamed logs
    print(line)
```

## Contents

- [Exe-free architecture](#exe-free-architecture)
- [Install](#install)
- [Quick start](#quick-start)
- [API reference](#api-reference)
- [CLI usage](#cli-usage)
- [Environment variables](#environment-variables)
- [Short-connection model (power friendly)](#short-connection-model-adbutils-style-power-friendly)
- [hdc socket protocol notes](#hdc-socket-protocol-notes)
- [adbutils mapping](#adbutils-mapping)
- [Testing](#testing)
- [Real device / emulator validation plan](#real-device--emulator-validation-plan)
- [Known limitations](#known-limitations)
- [Reference sources](#reference-sources)

## Exe-free architecture

adbutils talks to the adb server (127.0.0.1:5037) in pure python and uses
external tools only for a few extras. hdcutils goes further: **all device
operations (the server command channel + the file/app wire protocols +
server process management) are implemented over plain sockets / pure
python**, so bundled-vs-PATH hdc binaries and SDK version churn cannot
affect them:

```
                        +--------------------------------------------+
                        |             hdcutils (pure python)         |
                        |                                            |
  HdcClient/HdcDevice --+  (1) command channel: direct to 8710       |
                        |      list targets / shell / interactive    |
                        |      shell / tconn / fport / hilog /       |
                        |      checkserver                           |
                        |                                            |
                        |  (2) file/app task protocol (pure socket): |
                        |      file send/recv (CHECK/BEGIN/DATA/     |
                        |      FINISH + protobuf-style TransferConfig|
                        |      and TransferPayload frames)           |
                        |      install (randomized name + bm install)|
                        |      uninstall (bm uninstall, result echo) |
                        |                                            |
                        |  (3) server management:                    |
                        |      kill_server reads $TMPDIR/.HDCServer  |
                        |      .pid (equivalent of official hdc kill)|
                        |                                            |
                        |  hdc.exe has exactly one optional role:    |
                        |  pulling the server up when it is absent   |
                        +--------------------------------------------+
```

Key points:

1. **While a server is running, the library never touches hdc.exe** -- a
   server started by DevEco, by command-line tools, or by any SDK version
   works the same (both 44/108-byte handshake variants are supported).
2. `kill_server` replicates the official implementation: `hdc kill` never
   touches the socket either; it reads `$TMPDIR/.HDCServer.pid` and SIGKILLs
   the PID (`src/host/client.cpp::KillServer/GetLastPID`). This library does
   the same with `os.kill`, falling back to a port-to-PID lookup.
3. A **cold start** of the server physically requires an hdc executable (it
   carries the USB transport). That is the only retained binary use; when no
   binary exists, start the server once from DevEco manually.

## Install

```bash
pip install -e .            # from this repo (no compilation)
pip install -e ".[pillow]"  # screenshots as PIL.Image (optional)
```

Python 3.8+; device side needs HarmonyOS NEXT (API 12+) with USB debugging
enabled. The hdc executable is only used to pull the server up when it is
not running
([how to get it](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/ide-hdc)).

## Quick start

```python
import hdcutils

hdc = hdcutils.HdcClient()                  # connects to 127.0.0.1:8710 by default
                                            # auto-pulls the server up if absent
d = hdc.device()                            # auto-select a single device
print(d.device_info())                      # DeviceInfo(serial=..., model=...)

# shell
d.shell("ls /data/local/tmp")
out, rc = d.shell2("param get const.product.model")   # (output, returncode)

# files (pure-socket file protocol)
d.send_file("local.bin", "/data/local/tmp/a.bin")
d.recv_file("/data/log/x.txt", "x.txt")
d.send_dir("local_dir", "/data/local/tmp/dir")          # per-file + mkdir
d.pull_dir("/data/log", "out_dir")
data = d.read_file("/data/local/tmp/small.bin")          # base64 over socket
d.write_file("/data/local/tmp/a.txt", b"hello")
d.sync.push("a.bin", "/data/local/tmp/a.bin")            # adbutils-style namespace

# apps (pure-socket app protocol)
d.install("app.hap", "-r")
d.uninstall("com.example.app")
d.app_start("com.example.app")
d.app_stop("com.example.app")
d.app_current().package                                   # hidumper foreground app
d.open_browser("https://example.com")                     # aa start -U

# input and power (uitest uiInput / power-shell)
d.click(400, 800)
d.swipe(400, 1600, 400, 400)
d.send_keys("hello")                                      # tap center, then type
d.keyevent(hdcutils.KeyCode.BACK)
d.screen_on(); print(d.is_screen_on()); d.unlock()
print(d.battery())                                        # hidumper BatteryService

# screenshot and resolution
img = d.screenshot()                       # PIL.Image (bytes without Pillow)
print(d.window_size())                     # WindowSize(width, height), rotation-aware

# port forwarding (entry point for device services such as uitest)
d.fport("tcp:17170", "tcp:8012")
print(d.fport_list())
d.fport_remove_all()
print(d.create_connection("tcp", 8012).recv(8))           # adbutils-style tunnel

# network devices
hdc.connect("192.168.1.3:10123")            # hdc tconn
hdc.disconnect("192.168.1.3:10123")

# server management
hdc.server_version()                        # checkserver (socket)
hdc.kill_server()                           # reads .HDCServer.pid, no exe
hdc.start_server()                          # the only place hdc.exe may be used

# interactive shell
with d.open_shell() as sh:
    sh.send("ps -ef")
    print(sh.recv(timeout=2))
```

Waiting for a device:

```python
d = hdc.wait_for_device(timeout=30)          # poll until any device is ready
d = hdc.wait_for_device("SN123456", timeout=30)
```

Multiple devices in parallel:

```python
for d in hdc.device_list():
    print(d.serial, d.shell("param get const.product.model"))
```

## API reference

### HdcClient(host='127.0.0.1', port=None, hdc_path=None, auto_start=True)

| Member | Description |
|---|---|
| `list_targets(verbose=False)` | Device list; `False` -> `[str]`, `True` -> `[TargetInfo]` |
| `device(serial=None)` | `HdcDevice`; single device auto-selected, multiple raises |
| `device_list()` / `list()` | All devices (adbutils-compatible aliases) |
| `connect(addr)` / `disconnect(addr)` | `hdc tconn [addr -remove]` |
| `wait_for_device(serial=None, timeout, poll_interval)` / `wait_for(...)` | Poll until a device is ready |
| `server_version()` | `checkserver`, returns the server version |
| `start_server()` / `kill_server()` / `restart_server()` | Server process management (kill is pure python) |
| `stream_command(cmd, serial=None, timeout=None)` | Run a command, yield raw chunks until EOF |

### HdcDevice(client, serial)

**shell / logs**

| Member | Description |
|---|---|
| `shell(cmd, stream=False, timeout=None)` | Run a command, return text; `cmd` is a str or list; `stream=True` yields raw chunks |
| `shell_bytes(cmd)` | Same, returning raw bytes |
| `shell2(cmd)` | Return `(output, returncode)` (via the `echo __RC__$?` trick) |
| `open_shell()` | Interactive shell session (`ShellSession.send/recv/close`) |
| `stream_shell(cmd)` / `stream_lines(cmd)` | Stream a command (frames / lines) |
| `hilog(*args, timeout=None)` / `logcat(...)` | Streamed device logs (line by line) |

**Files (pure-socket file protocol)**

| Member | Description |
|---|---|
| `send_file(local, remote, timeout, hold_timestamp, update_if_new)` | Push a file (`-a`/`-sync` options) |
| `recv_file(remote, local, timeout)` | Pull a file |
| `send_dir(local_dir, remote_dir)` / `pull_dir(remote_dir, local_dir)` | Directory transfer (per file + mkdir) |
| `read_file(remote)` / `write_file(remote, data)` | base64 over socket, small files |
| `sync.push/pull/read_bytes/read_text/write_bytes/write_text/iter_content` | adbutils-style namespace |
| `install(path, *args, timeout=600)` | `hdc install [-r] <path>` (pure-socket app protocol) |
| `uninstall(bundle, keep_data=False)` | `hdc uninstall [-k]` |

**Apps**

| Member | Description |
|---|---|
| `list_apps()` / `list_packages()` | `bm dump -a` bundle names |
| `app_info(bundle)` | `bm dump -n`; dict when parseable JSON |
| `app_version(bundle)` | versionName |
| `app_start(bundle, ability=None, url=None)` | `aa start -b ... [-a ...] [-U ...]` |
| `open_browser(url)` / `open_url(url)` | `aa start -U`, schema/browser launch |
| `app_stop(bundle)` / `app_clear(bundle)` | `aa force-stop` / `bm clean -d` |
| `app_current()` | Foreground app (`AppCurrentInfo`, hidumper AbilityManagerService) |

**Input / power / info**

| Member | Description |
|---|---|
| `click/double_click/long_click(x, y)` | `uitest uiInput` tap family |
| `swipe(x1,y1,x2,y2,speed=None)` / `drag(...)` | Swipe / drag |
| `keyevent(key)` | `uitest uiInput keyEvent`; `KeyCode` (Back/Home/VolumeUp/VolumeDown/Power/Menu), int, or name |
| `send_keys(text, x=None, y=None)` / `input_text(...)` | `uitest uiInput inputText`; taps the screen center first without coordinates |
| `volume_up()/volume_down()` | Volume keys |
| `screen_on()/screen_off()/is_screen_on()/unlock()` | `power-shell wakeup/suspend` + hidumper + swipe-up unlock |
| `battery()` | `hidumper -s BatteryService` (capacity / charge state) |
| `get_prop(name)` / `get_props()` | `param get` system parameters |
| `device_info()` | `DeviceInfo` (model/brand/os_version/api_version ...) |
| `window_size()` | Screen resolution, parsed from the screenshot JPEG SOF marker |
| `reboot(mode=None)` | Reboot (`bootloader`/`recovery`) |
| `screenshot(save_path=None, display_id=None)` | Screenshot: `snapshot_display -f`, falling back to `uitest screenCap` |
| `wait_for_device(timeout, poll_interval)` | Wait until this device is online |

**Port forwarding**

| Member | Description |
|---|---|
| `fport(local, remote)` / `rport(remote, local)` | forward / reverse rules (aliases: `forward`/`reverse`) |
| `fport_list()` / `forward_list()` | List rules |
| `fport_remove(rule)` / `forward_remove(rule)` / `fport_remove_all()` | Remove rules |
| `create_connection(what, port)` | adbutils-style tunnel (`"tcp"` or `"unix"`/`localabstract`); the rule is removed on socket close |
| `root()` | `hdc smode` (root-mode daemon) |
| `tcpip(port=10123)` | `hdc tmode port <port>`, then `client.connect("ip:port")` |

## CLI usage

```
python -m hdcutils list [-v]
python -m hdcutils -t <serial> shell param get const.product.model
python -m hdcutils push local.hap /data/local/tmp/app.hap
python -m hdcutils pull /data/local/tmp/x.jpeg x.jpeg
python -m hdcutils install app.hap
python -m hdcutils screenshot screen.jpeg
python -m hdcutils fport ls
python -m hdcutils tconn 192.168.1.3:10123
python -m hdcutils checkserver
python -m hdcutils version
```

Global flags: `-t/--target`, `--port`, `--host`, `--hdc-path` (only used to
auto-start the server), `--no-auto-start`.

## Environment variables

| Variable | Description |
|---|---|
| `HDC_SERVER_PORT` / `OHOS_HDC_SERVER_PORT` | hdc server port (commercial / open-source builds); client and server must agree |
| `HDCUTILS_HDC_SERVER_PORT` | hdcutils-specific override (highest priority) |
| `HDCUTILS_HDC_PATH` | hdc executable path (**only used to auto-start the server**) |

## Short-connection model (adbutils-style, power-friendly)

The communication model was cross-checked against three sources:

* **hmdriver2 / hmnextauto**: every command spawns an hdc CLI subprocess that
  exits after answering; only fport + the device-side uitest agent (8012)
  keeps a long connection;
* **official docs**: hdc commands are one-shot CLI invocations, no resident
  session semantics;
* **adbutils**: one TCP connection per command to the adb server, closed
  once the response arrives; the library holds no persistent socket.

hdcutils follows the same model and tightens it further:

| Scenario | Connection behavior |
|---|---|
| All ordinary APIs (shell / file / install / fport ...) | **one connection per command**, closed as soon as the response is complete (EOF or single-frame done); no lingering objects |
| list targets / checkserver / tconn | Single-frame responses (verified in source) -- **done on first frame** (80ms window), no idle tail |
| wait_for_device | Polling is owned by the library; each round is one short connection, no background threads |
| `hilog()` / `open_shell()` / `stream_command()` | **explicit long connections** (streaming scenarios); close them between capture sessions |
| Liveness probing | 30s cache; no per-command probe, no heartbeats, no background polling |

Power notes: the library runs **no background threads, heartbeats or
persistent sockets**; a connection stays open only while you consume a
streaming API. For long log captures, close the generator between sessions
so the device-side daemon session is released promptly.

## hdc socket protocol notes

> The hdc client<->server protocol has **no official wire documentation**;
> everything below was reverse-engineered from the open-source hdc sources
> and verified end-to-end against a protocol-faithful mock server. Ground
> truth: [openharmony/developtools_hdc](https://github.com/openharmony/developtools_hdc)
> -- `src/common/channel.h` (ChannelHandShake), `src/common/channel.cpp`
> (ReadStream), `src/common/define.h`, `src/host/server_for_client.cpp`,
> `src/common/file.cpp`, `src/common/transfer.cpp`,
> `src/common/serial_struct_define.h`, `src/host/main.cpp`
> (AppendCwdWhenTransfer), `src/daemon/daemon_app.cpp`.

**Handshake** (44 bytes, pragma pack(1); version-check builds send 108 =
44 + 64 bytes of version text):

```
server -> client:
  [0..7]   banner  "OHOS HDC"
  [10]     'K'  (SERVICE_KILL_TAG)   [11] 'H'  (HUGE_BUF_TAG)
  [12..15] channelId u32 big-endian   [16..43] zero
  [44..107] (optional) version string

client -> server (44 bytes):
  [0..11]  banner echo (including both tags)
  [12..43] connectKey ASCII, zero-padded (device serial or "any")
```

The server validates only the 8-byte banner and the connectKey length --
**no version check** -- so a custom client replying with 44 bytes works
with both server variants.

**Frame format** (uniform after the handshake):
`[4-byte big-endian length][payload]`. The client's first frame payload is
the command text + `\0`; file/app task frames are `[u16 LE command][data]`;
log text travels as plain text frames.

**file/app task protocol** (implemented here in pure socket, replacing the
hdc CLI file channel):

- The official CLI appends ` remote -cwd "<cwd>"` to `file send/recv` and
  `install` command texts; the server then echoes
  `CMD_FILE_INIT`/`CMD_APP_INIT` back to the client (client-hosted task
  mode) and relays file/app frames verbatim afterwards;
- `file send`: client (master) sends `CMD_FILE_CHECK` (TransferConfig,
  protobuf-style serialization: fileSize/path/optionalName/options ...)
  -> daemon (slave) opens the remote file and replies `CMD_FILE_BEGIN`
  (8 feature-flag bytes) -> client streams `CMD_FILE_DATA` (64-byte
  `TransferPayload` prefix + data) -> sends `CMD_FILE_FINISH(1)` ->
  waits for the daemon's `CMD_FILE_FINISH(0)`;
- `file recv`: daemon (master) sends `CMD_FILE_CHECK` -> client (slave)
  opens the local file and replies `CMD_FILE_BEGIN` -> receives
  `CMD_FILE_DATA` frames and writes at the given index -> waits for
  `CMD_FILE_FINISH(1)` -> replies `CMD_FILE_FINISH(0)`;
- `install`: client sends `CMD_APP_CHECK` (randomized optionalName to avoid
  illegal package names) -> the daemon stores the payload under
  `/data/local/tmp/<random>.hap` and runs `bm install <options> -p <path>`,
  reporting `CMD_APP_FINISH`: `[mode u8][ok u8][msg]`;
- `uninstall`: the text command goes straight through (server-side task ->
  daemon `bm uninstall -n <bundle>`).

**Command lifetime** (drives response-completion detection): daemon-side
commands (shell etc.) end with the daemon sending
`CMD_KERNEL_CHANNEL_CLOSE`, after which the server closes the client
connection -- **EOF means done**; local commands (list targets etc.) keep
the connection open, so the library decides completion by idleness; the
interactive shell (`shell` with no arguments) enters
`interactiveShellMode` where every frame payload is treated as stdin and
`exit` ends the session.

## adbutils mapping

| adbutils | hdcutils |
|---|---|
| `adb.device_list()` | `hdc.list_targets()` |
| `adb.device(serial)` | `hdc.device(serial)` |
| `d.shell(cmd)` / `d.shell2(cmd)` | same names, same semantics |
| `d.sync.push/pull` | `d.sync.push/pull` (pure socket) |
| `d.install/uninstall` | `d.install/uninstall` (pure socket) |
| `d.forward/reverse` | `d.fport/rport` (aliases `forward`/`reverse`) |
| `d.create_connection(...)` | `d.create_connection(what, port)` |
| `d.screenshot()` | `d.screenshot()` |
| `d.logcat()` | `d.logcat()` (hilog) |
| `d.prop.*` | `d.prop.get()` / `d.get_props()` |
| `d.click/swipe/drag/send_keys/keyevent` | same names (uitest uiInput) |
| `d.window_size()` | `d.window_size()` (screenshot SOF parse) |
| `d.app_current()` | `d.app_current()` (hidumper) |
| `d.root()` / `d.tcpip()` | `d.root()` (smode) / `d.tcpip()` (tmode port) |
| `adb.connect/disconnect` | `hdc.connect/disconnect` |
| `adb.wait_for(state='device')` | `hdc.wait_for()` |

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v          # 243 tests, no device and no hdc.exe required
```

`tests/_mock.py` implements a **mock hdc server + emulated daemon** strictly
following the protocol above, covering:

- both 44/108-byte handshake variants (handshakes are length-framed, matching
  real servers);
- daemon/local command lifetimes, interactive shell, u16-prefixed frames;
- the **full file/app task protocol** (TransferConfig/TransferPayload
  protobuf encode/decode reuses the library's own implementation -- an
  implicit consistency check): single-file send/recv, directories,
  install, uninstall;
- the PID-file kill path and auto-pullup (a fake hdc launches a real mock
  server process);
- **per-API coverage** (`tests/test_api_coverage.py`): every public method of
  `HdcClient` / `HdcDevice` / `HdcSync` / `Prop` is exercised -- shell family,
  files, apps, input (asserting the exact `uitest uiInput` command shapes),
  screen/power, fport/tunnels, exceptions and aliases.

### Validated against real hdc servers

The library has been verified against two independent real hdc
implementations: the **official OpenHarmony 6.1 release toolchain hdc.exe**
(sha256-verified SDK) and [muka_rust_hdc](https://github.com/Attect/muka_rust_hdc).
That validation uncovered and fixed two wire-level details that mocks alone
could not:

1. the client/server handshake is **length-framed in both directions**
   (`[4B BE length][44 or 108-byte handshake]`);
2. `list targets -v` reports UART/COM probe entries and unauthorized devices
   as `Ready` -- only `Connected` is a usable target, so
   `device()`/`wait_for_device()` ignore `Ready` entries.

## Examples

Runnable examples live in [`examples/`](examples/):

```bash
python examples/01_quickstart.py        # connect, enumerate, shell, props
python examples/02_files.py             # send/recv/read/write, sync namespace, dirs
python examples/03_apps.py              # install/uninstall/list/info/start/stop
python examples/04_input_screen.py      # screenshot, window size, tap/swipe/keys
python examples/05_network.py           # USB -> WiFi (tmode/tconn), forwarding, tunnels
python examples/06_logs_and_streams.py  # hilog/logcat, streaming and interactive shell
python examples/07_acceptance.py        # 13-step real-device acceptance run
```


## Real device / emulator validation plan

Protocol mocks cannot cover real device-side behavior, so validate against a
**real server + device**. The server side is already validated (see the
testing section: official OpenHarmony 6.1 hdc toolchain + muka_rust_hdc), so
what remains is the device side. In order of preference:

0. **Already on this machine** (see `D:\HarmonyOS\README.md`): the official
   `hdc.exe` from the sha256-verified OpenHarmony 6.1 SDK is deployed with
   `HDCUTILS_HDC_PATH`/PATH set, the USB driver pack is at
   `D:\HarmonyOS\usb-driver`, and `python examples/07_acceptance.py` runs the
   13-step check against whatever device is attached;
1. **First choice: the official DevEco Studio emulator** (no real phone needed):
   1. Install DevEco Studio -> Device Manager -> create an API 12+
      (HarmonyOS NEXT) emulator and start it;
   2. DevEco's bundled hdc server starts automatically; verify with
      `python -m hdcutils list -v` (should show `<serial> EMULATOR Connected`);
   3. Walk through the acceptance script below;
2. **Second: a real phone** (Mate 60 series / any HarmonyOS NEXT device with
   USB debugging on): same acceptance script; `fport`/screenshot/install are
   closest to production on real hardware;
3. **Also possible: open-source OpenHarmony boards** plus the
   [OpenHarmony SDK hdc](https://github.com/openharmony/developtools_hdc).

Acceptance script:

```python
import hdcutils

hdc = hdcutils.HdcClient()
print("server:", hdc.server_version())            # 1. checkserver
print("targets:", hdc.list_targets(verbose=True)) # 2. device enumeration
d = hdc.device()
print(d.device_info())                             # 3. param/shell
print(d.shell2("echo hello"))                      # 4. shell + exit code
d.send_file("local.bin", "/data/local/tmp/hdcutils_e2e.bin")   # 5. big-file send
print(d.recv_file("/data/local/tmp/hdcutils_e2e.bin", "back.bin"))  # 6. recv
d.install("something.hap")                          # 7. install (any hap on emulator)
print(d.app_version("bundle.name"))
print(d.app_current())                              # 8. foreground app
d.fport("tcp:17170", "tcp:8012"); print(d.fport_list()); d.fport_remove_all()  # 9. forward
d.screenshot("shot.jpeg")                           # 10. screenshot
print(d.window_size())                              # 11. resolution
d.keyevent("Back")                                  # 12. input
list(d.hilog(timeout=5))                            # 13. streamed logs
```

If a step fails, first rule out server-version factors: run `hdc kill`, then
retry with `HDCUTILS_HDC_PATH` pointing at the emulator's hdc. Device
operations are version-independent; failures are most likely protocol timing
differences -- file an issue with the `hdc -l5` log attached.

## Known limitations

1. **Not implemented by design**: lz4 compression (-z), app sandbox (-b
   bundlename), and the binary directory-mode protocol; directory transfers
   are composed from single-file sends + mkdir (the single-file protocol is
   byte-identical).
2. **No official protocol documentation, and it evolves with versions**: the
   client supports both 44/108-byte handshakes; our client does not
   participate in version checking (the server does not check the client
   version).
3. **Coexisting hdc servers**: a DevEco-bundled hdc and a PATH hdc with
   mismatched versions is the most common failure (the server's global mutex
   is port-independent). Run `kill_server()` first, or align
   `HDC_SERVER_PORT`.
4. hdc itself does **not propagate shell exit codes**; `shell2` obtains them
   via the `echo __RC__$?` trick, which can fail on exotic shell syntax.
5. On very old images `snapshot_display` does not exist; the library falls
   back to `uitest screenCap`. `battery/is_screen_on/app_current` depend on
   hidumper output formats -- parse failures raise with the raw output
   attached instead of failing silently.

## Reference sources

The protocol understanding and API design are based on the following open
source projects -- many thanks:

| Source | Role |
|---|---|
| [openatx/adbutils](https://github.com/openatx/adbutils) | **API design and layering blueprint**: HdcClient/HdcDevice mirror AdbClient/AdbDevice; shell, file, install, screenshot, wait_for, prop and app-series semantics all align |
| [codematrixer/hmdriver2](https://github.com/codematrixer/hmdriver2) | Pioneer of HarmonyOS python automation: fport + on-device uitest agent (8012), the `HDC_SERVER_PORT` convention, and the uitest uiInput command shapes |
| [ziguiway/hmnextauto](https://github.com/ziguiway/hmnextauto) | Actively maintained hmdriver2 superset: uitest_agent push/launch/update, hidumper performance collection, real-world HarmonyOS experience |
| [openharmony/developtools_hdc](https://github.com/openharmony/developtools_hdc) | **The de-facto source of truth for the hdc wire protocol** (Apache-2.0): handshake struct, 4-byte big-endian framing, `HdcCommand` numbers, command lifetimes (CMD_KERNEL_CHANNEL_CLOSE), the file/app task protocol (common/file.cpp, transfer.cpp, host_app.cpp, daemon_app.cpp), the server PID file (`.HDCServer.pid`), and the `AppendCwdWhenTransfer` `remote -cwd` convention |
| [Attect/muka_rust_hdc](https://github.com/Attect/muka_rust_hdc) | Complete reverse-engineered protocol write-up (`docs/HDC_SERVER_SOCKET_PROTOCOL.md`); its independent Rust implementation proves the protocol can be reimplemented and coexist with DevEco; informed the 108-byte handshake compatibility design |
| [codematrixer/awesome-hdc](https://github.com/codematrixer/awesome-hdc) | hdc command manual and engineering notes (multi-server conflicts, `hdc kill -r` troubleshooting, etc.) |
| [openatx/uiautomator2](https://github.com/openatx/uiautomator2) | The device-agent + host-driver layering reference (no UI automation layer here; can be extended via fport + uitest agent) |

## License

MIT
