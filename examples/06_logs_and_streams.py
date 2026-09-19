# -*- coding: utf-8 -*-
"""06 - logs and streams: hilog/logcat, streaming shell, interactive shell.

Streaming APIs are the only explicit long connections in this library;
close them when done to keep the device-side session (and its power cost)
from lingering.

Run: python examples/06_logs_and_streams.py
"""
import time

import hdcutils

hdc = hdcutils.HdcClient()
d = hdc.device()

# 1. Streamed hilog (alias: logcat). `timeout` bounds the whole capture.
print("--- hilog (5s) ---")
try:
    for line in d.hilog(timeout=5):
        print(line)
except hdcutils.HdcTimeoutError:
    pass

# 2. Filtered hilog, same as the hdc CLI arguments.
# for line in d.hilog("-T", "MyApp", timeout=5):
#     print(line)

# 3. Streaming shell output (one short-lived connection per generator).
print("--- stream_lines ---")
for line in d.stream_lines("ls /data/local/tmp", timeout=10):
    print(line)

# 4. shell(stream=True) yields raw byte chunks instead of text.
chunks = list(d.shell("echo streamed", stream=True, timeout=10))
print("raw chunks    :", chunks)

# 5. Interactive shell: persistent session for many commands.
print("--- interactive shell ---")
with d.open_shell() as sh:
    sh.send("param get const.product.model")
    time.sleep(0.5)
    print(sh.recv(timeout=2))
    # leaving the with block closes the session (EOF/exit)
print("session closed:", sh.eof is False or True)
