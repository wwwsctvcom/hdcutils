path = "tests/test_api_coverage.py"
src = open(path, encoding="utf-8").read()

old = '''def test_shell_str_and_list(device):
    assert device.shell("echo hi") == "hi"
    assert device.shell(["echo", "hi"]) == "hi"
    assert device.shell("shell echo hi") == "hi"  # double prefix tolerated


def test_shell_bytes_and_shell2(device):
    assert device.shell_bytes("echo raw") == b"raw\r\n"
    out, rc = device.shell_ex("echo hi")
    assert (out, rc) == ("hi", 0)


def test_shell_stream_and_stream_lines(device):
    chunks = list(device.shell("echo streamed", stream=True, timeout=10))
    assert b"".join(chunks).strip() == b"streamed"
    lines = list(device.stream_lines("hilog", timeout=10))
    assert lines == ["hilog line 0", "hilog line 1", "hilog line 2"]'''

new = '''def test_shell_is_the_single_entry_point(device):
    """One shell API (like hypium's `driver.shell`), plus documented extras."""
    assert device.shell("echo hi") == "hi"
    assert device.shell(["echo", "hi"]) == "hi"
    assert device.shell("shell echo hi") == "hi"  # double prefix tolerated
    # no stream= keyword: streaming has its own explicit method
    import inspect

    assert "stream" not in inspect.signature(device.shell).parameters


def test_shell_bytes_and_shell_ex(device):
    assert device.shell_bytes("echo raw") == b"raw\r\n"
    out, rc = device.shell_ex("echo hi")
    assert (out, rc) == ("hi", 0)
    assert not hasattr(device, "shell2")          # renamed to shell_ex


def test_stream_shell(device):
    """stream_shell is the only streaming entry (long connection)."""
    chunks = list(device.stream_shell("echo streamed", timeout=10))
    assert b"".join(chunks).strip() == b"streamed"
    frames = list(device.stream_shell("hilog", timeout=10))
    assert len(frames) == 3
    assert not hasattr(device, "stream_lines")    # split lines yourself'''

assert old in src
src = src.replace(old, new)
open(path, "w", encoding="utf-8", newline="").write(src)

# test_client.py 里的 stream_lines 测试也改
path = "tests/test_client.py"
src = open(path, encoding="utf-8").read()
old2 = '''def test_stream_lines(client):
    d = client.device("MOCKSERIAL1")
    lines = list(d.stream_lines("hilog", timeout=10))
    assert len(lines) == 3'''
new2 = '''def test_stream_shell_frames(client):
    d = client.device("MOCKSERIAL1")
    frames = list(d.stream_shell("hilog", timeout=10))
    assert len(frames) == 3'''
assert old2 in src
src = src.replace(old2, new2)
src = src.replace("def test_shell2_returncode(client):", "def test_shell_ex_returncode(client):")
open(path, "w", encoding="utf-8", newline="").write(src)
print("tests updated")
