# hdcutils examples

Runnable examples against a **real** hdc server (and a real device when one
is attached). Setup once:

```bash
# Windows (this machine): hdc tool + library are already deployed
setx HDCUTILS_HDC_PATH "D:\HarmonyOS\hdc\hdc.exe"
pip install -e ..
```

Every example prints what it does and skips device-dependent steps when no
device is attached, so they are safe to run in order:

```bash
python examples/01_quickstart.py        # connect, enumerate, shell, props
python examples/02_files.py             # send/recv/read/write, sync namespace, dirs
python examples/03_apps.py              # install/uninstall/list/info/start/stop/current
python examples/04_input_screen.py      # screenshot, window size, tap/swipe/keys
python examples/05_network.py           # USB -> WiFi (tmode/tconn), port forwarding, tunnels
python examples/06_logs_and_streams.py  # hilog/logcat, streaming shell, interactive shell
python examples/07_acceptance.py        # full real-device acceptance run (13 steps)
```

Most examples need a connected device; `01` and `05` also demonstrate the
no-device paths (enumeration, error handling, server management).
