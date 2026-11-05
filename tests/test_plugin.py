import json

import pytest

CYCLES = """
import gc

class Leaky:
    def __init__(self):
        self.me = self
        self.payload = [object() for _ in range(10)]

def make():
    for _ in range(50):
        Leaky()
"""


def test_inactive_by_default(pytester):
    pytester.makepyfile("def test_a():\n    pass\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*= gc-steward =*")


def test_freeze_moves_startup_objects_to_permanent_generation(pytester):
    pytester.makepyfile(
        """
        import gc

        def test_frozen():
            assert gc.get_freeze_count() > 0
        """
    )
    result = pytester.runpytest("--gc-freeze")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*gc-steward*", "main: 1 tests*frozen [1-9]*"])


def test_gc_every_forces_collections(pytester):
    pytester.makepyfile("\n".join(f"def test_{i}():\n    pass\n" for i in range(10)))
    result = pytester.runpytest("--gc-every", "3")
    result.assert_outcomes(passed=10)
    result.stdout.fnmatch_lines(["main: 10 tests*forced 3x*"])


def test_ini_options(pytester):
    pytester.makeini("[pytest]\ngc_steward_every = 2\n")
    pytester.makepyfile("\n".join(f"def test_{i}():\n    pass\n" for i in range(4)))
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(["main: 4 tests*forced 2x*"])


def test_cli_overrides_ini(pytester):
    pytester.makeini("[pytest]\ngc_steward_every = 2\n")
    pytester.makepyfile("\n".join(f"def test_{i}():\n    pass\n" for i in range(4)))
    result = pytester.runpytest("--gc-every", "4")
    result.stdout.fnmatch_lines(["main: 4 tests*forced 1x*"])


def test_disable_turns_off_automatic_gc_during_tests_only(pytester):
    pytester.makepyfile(
        """
        import gc

        def test_off():
            assert not gc.isenabled()
        """
    )
    result = pytester.runpytest("--gc-disable", "--gc-every", "1")
    result.assert_outcomes(passed=1)


def test_rss_growth_triggers_collection(pytester):
    pytester.makepyfile(
        """
        import gc

        keep = []

        def test_grow():
            keep.append(bytearray(64 * 1024 * 1024))

        def test_after():
            pass
        """
    )
    result = pytester.runpytest_subprocess("--gc-rss-growth", "32")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["main: 2 tests*forced [1-9]x*"])


def test_report_counts_cycle_types_per_test(pytester):
    pytester.makepyfile(
        test_cycles=CYCLES
        + """
def test_leaks():
    make()

def test_clean():
    [object() for _ in range(100)]
"""
    )
    report = pytester.path / "gc.json"
    result = pytester.runpytest("--gc-report", str(report))
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        ["*objects reclaimed by the cyclic GC, by type*", "*test_cycles.Leaky*"]
    )
    data = json.loads(report.read_text())
    merged = data["merged"]
    assert merged["types"]["test_cycles.Leaky"] == 50
    assert "test_cycles.py::test_leaks" in merged["tests_top"]
    assert "test_cycles.py::test_clean" not in merged["tests_top"]


def test_xdist_workers_are_aggregated(pytester):
    pytest.importorskip("xdist")
    pytester.makepyfile(
        test_cycles=CYCLES
        + "\n".join(f"\ndef test_{i}():\n    make()\n" for i in range(6))
    )
    report = pytester.path / "gc.json"
    result = pytester.runpytest("-n", "2", "--gc-report", str(report))
    result.assert_outcomes(passed=6)
    result.stdout.fnmatch_lines(["gw0: * tests*", "gw1: * tests*"])
    data = json.loads(report.read_text())
    assert set(data["processes"]) == {"gw0", "gw1"}
    assert data["merged"]["types"]["test_cycles.Leaky"] == 300
