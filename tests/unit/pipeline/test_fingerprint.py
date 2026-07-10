"""错误指纹归一化测试。"""

from aiteam.pipeline.fingerprint import fingerprint, normalize


class TestNormalize:
    def test_strips_iso_timestamp(self):
        msg = "failed at 2026-04-28T10:30:00Z during startup"
        result = normalize(msg)
        assert "2026-04-28" not in result
        assert "<TS>" in result

    def test_strips_iso_timestamp_with_offset(self):
        msg = "error at 2026-04-28T10:30:00.123+08:00"
        result = normalize(msg)
        assert "2026" not in result
        assert "<TS>" in result

    def test_strips_unix_timestamp(self):
        msg = "failed timestamp=1714262400 in worker"
        result = normalize(msg)
        assert "1714262400" not in result
        assert "<TS>" in result

    def test_strips_unix_path(self):
        msg = "cannot open /home/user/app/config.py"
        result = normalize(msg)
        assert "/home/user/app/config.py" not in result
        assert "<PATH>" in result

    def test_strips_windows_path(self):
        msg = r"cannot open C:\Users\foo\app\config.py"
        result = normalize(msg)
        assert "Users" not in result
        assert "<PATH>" in result

    def test_strips_pid(self):
        msg = "process 12345 killed"
        result = normalize(msg)
        assert "12345" not in result
        assert "<PID>" in result

    def test_strips_memory_address(self):
        msg = "segfault at 0x7fff1234abcd"
        result = normalize(msg)
        assert "0x7fff1234abcd" not in result
        assert "<ADDR>" in result

    def test_preserves_error_type(self):
        msg = "ValueError: index out of range"
        result = normalize(msg)
        assert "ValueError" in result
        assert "index out of range" in result


class TestFingerprint:
    def test_same_error_different_timestamps_same_fingerprint(self):
        fp1 = fingerprint("DBError", "timeout at 2026-04-28T10:00:00Z")
        fp2 = fingerprint("DBError", "timeout at 2026-04-28T11:30:00Z")
        assert fp1 == fp2

    def test_same_error_different_paths_same_fingerprint(self):
        fp1 = fingerprint("IOError", "cannot open /tmp/foo/data.csv")
        fp2 = fingerprint("IOError", "cannot open /var/log/app/data.csv")
        assert fp1 == fp2

    def test_different_error_types_different_fingerprints(self):
        fp1 = fingerprint("DBError", "connection refused")
        fp2 = fingerprint("NetworkError", "connection refused")
        assert fp1 != fp2

    def test_different_messages_different_fingerprints(self):
        fp1 = fingerprint("ValueError", "index out of range")
        fp2 = fingerprint("ValueError", "division by zero")
        assert fp1 != fp2

    def test_fingerprint_length_12(self):
        fp = fingerprint("ValueError", "something went wrong")
        assert len(fp) == 12

    def test_fingerprint_is_hex(self):
        fp = fingerprint("ValueError", "something went wrong")
        int(fp, 16)  # raises if not valid hex

    def test_same_error_different_pids_same_fingerprint(self):
        fp1 = fingerprint("OSError", "process 1234 killed")
        fp2 = fingerprint("OSError", "process 9999 killed")
        assert fp1 == fp2

    def test_same_error_different_addresses_same_fingerprint(self):
        fp1 = fingerprint("SegFault", "crash at 0xdeadbeef")
        fp2 = fingerprint("SegFault", "crash at 0x00001234")
        assert fp1 == fp2
