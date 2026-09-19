# -*- coding: utf-8 -*-
"""03 - apps: list, inspect, install, start/stop/clear, uninstall.

Run: python examples/03_apps.py [package.hap]
"""
import sys

import hdcutils

hdc = hdcutils.HdcClient()
d = hdc.device()

# 1. Installed packages.
apps = d.list_apps()          # alias: list_apps()
print("installed apps: %d, first five: %s" % (len(apps), apps[:5]))

# 2. App details (bm dump -n) - dict when parseable JSON.
if apps:
    bundle = apps[0]
    info = d.app_info(bundle)
    print("app_info(%s): %s" % (bundle, str(info)[:120]))
    print("app_version  :", d.app_version(bundle))

# 3. Foreground app (hidumper AbilityManagerService).
try:
    current = d.app_current()
    print("foreground   :", current.package, "/", current.activity)
except hdcutils.HdcCommandError as exc:
    print("app_current unavailable:", exc)

# 4. Install a package when one is given (pure-socket app protocol:
#    randomized remote name -> bm install -p).
if len(sys.argv) > 1:
    hap = sys.argv[1]
    print("install      :", d.install(hap, "-r"))
    bundle = ""
    result = d.install(hap)  # returns daemon echo; the bundle name comes from the app
    print("install again:", result)

# 5. Start / stop / clear an app (official aa/bm commands). The official test
#    framework's spellings work too: start_app / stop_app / has_app /
#    clear_app_data / install_app / uninstall_app.
if apps:
    bundle = apps[0]
    try:
        d.aa_start(bundle)
        print("app_start ok :", bundle)
        d.aa_force_stop(bundle)
        print("app_stop  ok :", bundle)
    except hdcutils.HdcCommandError as exc:
        print("start/stop failed (system app?):", exc)

# 6. Open a URL through the system route (official `aa start -U <url>`).
# d.aa_start("com.example.browser", url="https://developer.huawei.com/")

# 7. Uninstall (careful: this really removes the app).
# d.uninstall("com.example.app", keep_data=False)
