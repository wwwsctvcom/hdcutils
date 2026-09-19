# -*- coding: utf-8 -*-
"""05 - network: USB -> WiFi migration (tmode/tconn), port forwarding, tunnels.

Run: python examples/05_network.py [phone-ip]
"""
import sys

import hdcutils

hdc = hdcutils.HdcClient()
print("server version:", hdc.server_version())

# ---------------------------------------------------------------- USB stage
targets = hdc.list_targets(verbose=True)
print("targets       :", targets)
if not targets:
    print("no device attached; plug one in over USB first")
    raise SystemExit(0)

d = hdc.device()
serial = d.serial

# ------------------------------------------------------- switch to TCP mode
# `tmode port <port>` restarts the daemon listening on TCP; the USB
# connection drops afterwards. adbutils-style name: d.tcpip(port).
PORT = 10123
print("tmode         :", d.tcpip(PORT))

# ------------------------------------------------------------ WiFi connect
ip = sys.argv[1] if len(sys.argv) > 1 else input("phone IP (e.g. 192.168.1.42): ").strip()
addr = "%s:%d" % (ip, PORT)
print("tconn         :", hdc.connect(addr))
print("targets now   :", hdc.list_targets())

wifi_device = hdc.device(addr)
print("wifi shell    :", wifi_device.shell("param get const.product.model"))

# ------------------------------------------------------------------ cleanup
print("disconnect    :", hdc.disconnect(addr))
# switch the daemon back to USB when needed:
#   hdc.device(serial).shell("tmode usb")

# ------------------------------------------------- port forwarding + tunnel
# fport/rport map ports on this machine to the device (adbutils: forward/reverse).
d.fport("tcp:17170", "tcp:8012")
print("fport list    :", d.fport_list())
d.fport_remove("tcp:17170 tcp:8012")

# create_connection opens a tunnel socket; the rule is removed on close.
# with d.create_connection("tcp", 8012) as sock:
#     print(sock.recv(64))
