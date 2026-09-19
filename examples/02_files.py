# -*- coding: utf-8 -*-
"""02 - files: push / pull / read / write, the sync namespace and directories.

Run: python examples/02_files.py
"""
import os
import tempfile

import hdcutils

hdc = hdcutils.HdcClient()
d = hdc.device()

workdir = tempfile.mkdtemp(prefix="hdcutils_example_")
local_file = os.path.join(workdir, "hello.bin")
with open(local_file, "wb") as f:
    f.write(bytes(range(256)) * 4)  # 1 KiB of binary data

REMOTE = "/data/local/tmp/hdcutils_example.bin"
REMOTE_BACK = os.path.join(workdir, "hello_back.bin")

# 1. Push a local file (pure-socket `file send` protocol: CHECK/BEGIN/DATA/FINISH).
print("send_file     :", d.send_file(local_file, REMOTE).splitlines()[:1])

# 2. Pull it back and verify.
print("recv_file     :", d.recv_file(REMOTE, REMOTE_BACK).splitlines()[:1])
print("roundtrip ok  :", open(local_file, "rb").read() == open(REMOTE_BACK, "rb").read())

# 3. Small-file helpers (base64 over a short shell connection).
data = d.read_file(REMOTE)
print("read_file     : %d bytes" % len(data))
d.write_file("/data/local/tmp/hdcutils_small.txt", b"hello from hdcutils\n")
print("write_file    :", d.read_file("/data/local/tmp/hdcutils_small.txt"))

# 4. adbutils-style sync namespace.
d.sync.push(local_file, "/data/local/tmp/hdcutils_sync.bin")
print("sync.read_text:", "peer ok" if d.sync.read_bytes(REMOTE) == data else "MISMATCH")
d.sync.pull("/data/local/tmp/hdcutils_sync.bin", os.path.join(workdir, "sync_back.bin"))

# 5. Directory transfer (composed per-file + mkdir; no binary dir-mode protocol).
local_dir = os.path.join(workdir, "tree")
os.makedirs(os.path.join(local_dir, "sub"), exist_ok=True)
open(os.path.join(local_dir, "a.txt"), "w").write("A")
open(os.path.join(local_dir, "sub", "b.txt"), "w").write("B")
count = d.send_dir(local_dir, "/data/local/tmp/hdcutils_tree")
print("send_dir      : %d files" % count)
out_dir = os.path.join(workdir, "tree_back")
count = d.pull_dir("/data/local/tmp/hdcutils_tree", out_dir)
print("pull_dir      : %d files" % count)

# 6. Cleanup.
d.shell("rm -rf /data/local/tmp/hdcutils_example.bin /data/local/tmp/hdcutils_sync.bin "
        "/data/local/tmp/hdcutils_small.txt /data/local/tmp/hdcutils_tree")
print("cleaned up; local artifacts in", workdir)
