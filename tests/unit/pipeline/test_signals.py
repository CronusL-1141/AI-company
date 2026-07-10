"""Pass/Fail 信号识别测试。"""

from aiteam.pipeline.signals import is_fail_signal, is_pass_signal


class TestIsPassSignal:
    def test_pytest_passed(self):
        assert is_pass_signal("5 passed in 0.12s", exit_code=0)

    def test_all_tests_passed(self):
        assert is_pass_signal("all tests passed", exit_code=0)

    def test_ok_tests(self):
        assert is_pass_signal("OK (3 tests)", exit_code=0)

    def test_checkmark_symbol(self):
        assert is_pass_signal("✓ everything ok", exit_code=0)

    def test_uppercase_pass(self):
        assert is_pass_signal("PASS", exit_code=0)

    def test_100_percent_passed(self):
        assert is_pass_signal("100% passed", exit_code=0)

    def test_build_successful(self):
        assert is_pass_signal("build successful", exit_code=0)

    def test_nonzero_exit_fails_even_with_pass_string(self):
        assert not is_pass_signal("5 passed", exit_code=1)

    def test_empty_stdout_not_pass(self):
        assert not is_pass_signal("", exit_code=0)

    def test_zero_exit_no_pass_pattern(self):
        assert not is_pass_signal("compilation complete", exit_code=0)


class TestIsFailSignal:
    def test_nonzero_exit_is_fail(self):
        assert is_fail_signal("", exit_code=1)

    def test_nonzero_exit_with_pass_string_still_fail(self):
        assert is_fail_signal("5 passed", exit_code=2)

    def test_traceback_in_stdout(self):
        assert is_fail_signal("Traceback (most recent call last):", exit_code=0)

    def test_error_keyword(self):
        assert is_fail_signal("SyntaxError: invalid syntax", exit_code=0)

    def test_failed_keyword(self):
        assert is_fail_signal("test_foo FAILED", exit_code=0)

    def test_clean_output_zero_exit_not_fail(self):
        assert not is_fail_signal("compilation complete", exit_code=0)

    def test_empty_stdout_zero_exit_not_fail(self):
        assert not is_fail_signal("", exit_code=0)

    def test_mixed_signal_fail_wins(self):
        # stdout has both pass and fail keywords; fail should win
        assert is_fail_signal("5 passed, 1 failed", exit_code=0)
