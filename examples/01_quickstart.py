# -*- coding: utf-8 -*-
"""01 - quick start: connect, enumerate devices, run shell, read parameters.

Run: python examples/01_quickstart.py
"""
import hdcutils

# 1. Connect to the hdc server (auto-pulls it up when absent; the hdc binary
#    is only used for that pull-up, never for device operations).
hdc = hdcutils.HdcClient()
print("server version:", hdc.server_version())

# 2. Enumerate devices. verbose=True returns TargetInfo entries.
print("targets       :", hdc.list_targets())
for target in hdc.list_targets(verbose=True):
    print("  detail      :", target)

# 3. Get a device handle. Without a serial: one device is auto-selected,
#    several raise (specify serial=...), none raises HdcDeviceNotFoundError.
try:
    d = hdc.device()
except hdcutils.HdcDeviceNotFoundError as exc:
    print("no device attached yet:", exc)
    print("plug a phone (USB debugging on) or start an emulator, then re-run")
    raise SystemExit(0)

print("device        :", d)

# 4. Shell: one short-lived connection per command.
print("model         :", d.shell("param get const.product.model"))
print("api version   :", d.get_prop("const.ohos.apiversion"))

# hdc does not report exit codes; shell2 adds the `echo __RC__$?` trick.
out, code = d.shell2("echo hello")
print("shell2        :", repr(out), "rc =", code)

# 5. Structured device info.
info = d.device_info()
print("device info   :", info)
print("  model=%s brand=%s os=%s api=%s"
      % (info.model, info.brand, info.os_version, info.api_version))

# 6. `param get` in raw form (param ls / set / wait / save are also available).
print("param get     :", d.param_get("const.product.model").strip())

# 7. Wait for a device to (re)appear, e.g. after reboot.
# d.target_boot(); d.wait(timeout=60)
