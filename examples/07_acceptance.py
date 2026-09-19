# -*- coding: utf-8 -*-
"""07 - real-device acceptance run: the 13-step validation checklist.

Prints PASS/FAIL/SKIP per step and a summary. Use it after connecting a
phone over USB or starting a DevEco emulator.

Run: python examples/07_acceptance.py [package.hap]
"""
import os
import sys
import tempfile
import traceback

import hdcutils

RESULTS = []


def run(number, title, func, hdc, ctx, optional=False):
    try:
        detail = func(hdc, ctx)
        state = "PASS"
    except SystemExit as exc:
        state, detail = ("SKIP" if optional else "SKIP"), str(exc) or "not available"
    except Exception as exc:  # noqa: BLE001 - report everything
        state, detail = "FAIL", "%s: %s" % (type(exc).__name__, exc)
        traceback.print_exc()
    RESULTS.append((number, title, state, detail))


# ----------------------------------------------------------------- steps 1-4
def step1(hdc, ctx):
    version = hdc.server_version()
    assert version, "empty server version"
    return version


def step2(hdc, ctx):
    targets = hdc.list_targets(verbose=True)
    if not targets:
        raise SystemExit("no device attached (plug a phone / start an emulator)")
    return "; ".join(str(t) for t in targets)


def step3(hdc, ctx):
    device = hdc.device()
    ctx["device"] = device
    return "%s / %s" % (device.serial, device.shell("param get const.product.model"))


def step4(hdc, ctx):
    out, code = ctx["device"].shell2("echo hello")
    assert code == 0, "rc=%s out=%r" % (code, out)
    return "rc=0 out=%r" % out


# ----------------------------------------------------------------- steps 5-6
def step5(hdc, ctx):
    local = os.path.join(tempfile.gettempdir(), "hdcutils_e2e.bin")
    with open(local, "wb") as f:
        f.write(bytes(range(256)) * 4096)  # 1 MiB
    ctx["local"] = local
    summary = ctx["device"].send_file(local, "/data/local/tmp/hdcutils_e2e.bin")
    return (summary.splitlines() or ["sent"])[0]


def step6(hdc, ctx):
    back = ctx["local"] + ".back"
    ctx["device"].recv_file("/data/local/tmp/hdcutils_e2e.bin", back)
    assert open(ctx["local"], "rb").read() == open(back, "rb").read(), "content mismatch"
    return "1 MiB roundtrip identical"


# ----------------------------------------------------------------- steps 7-9
def step7(hdc, ctx):
    if len(sys.argv) < 2:
        raise SystemExit("pass a .hap path to test install")
    result = ctx["device"].install(sys.argv[1], "-r")
    return (result.splitlines() or ["installed"])[0]


def step8(hdc, ctx):
    current = ctx["device"].app_current()
    return "%s / %s" % (current.package, current.activity)


def step9(hdc, ctx):
    device = ctx["device"]
    device.fport("tcp:17170", "tcp:8012")
    rules = device.fport_list()
    device.fport_remove("tcp:17170 tcp:8012")
    return "rules during test: %s" % rules


# ---------------------------------------------------------------- steps 10-13
def step10(hdc, ctx):
    data = ctx["device"].screenshot_data()
    assert data[:2] == b"\xff\xd8", "not a JPEG stream"
    return "%d bytes JPEG" % len(data)


def step11(hdc, ctx):
    size = ctx["device"].window_size()
    return "%sx%s" % (size.width, size.height)


def step12(hdc, ctx):
    ctx["device"].key_event("Back")
    return "uitest uiInput keyEvent Back"


def step13(hdc, ctx):
    lines = []
    try:
        for line in ctx["device"].hilog(timeout=5):
            lines.append(line)
            if len(lines) >= 3:
                break
    except hdcutils.HdcTimeoutError:
        pass
    return "%d lines in 5s" % len(lines)


STEPS = [
    (1, "checkserver", step1),
    (2, "device enumeration (list targets -v)", step2),
    (3, "device handle + shell", step3),
    (4, "shell2 exit code trick", step4),
    (5, "file send (~1MiB, pure socket)", step5),
    (6, "file recv + byte-exact roundtrip", step6),
    (7, "install package (pure socket)", step7),
    (8, "foreground app (hidumper)", step8),
    (9, "port forwarding (fport)", step9),
    (10, "screenshot", step10),
    (11, "window_size", step11),
    (12, "input injection (uitest uiInput keyEvent)", step12),
    (13, "streamed hilog", step13),
]


def main():
    hdc = hdcutils.HdcClient()
    ctx = {}
    for number, title, func in STEPS:
        run(number, title, func, hdc, ctx)

    print("\n%-4s %-42s %-6s %s" % ("#", "step", "state", "detail"))
    print("-" * 100)
    for number, title, state, detail in RESULTS:
        print("%-4s %-42s %-6s %s" % (number, title, state, str(detail)[:62]))
    print("-" * 100)
    passed = len([r for r in RESULTS if r[2] == "PASS"])
    skipped = len([r for r in RESULTS if r[2] == "SKIP"])
    failed = len([r for r in RESULTS if r[2] == "FAIL"])
    print("%d passed, %d skipped, %d failed" % (passed, skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
