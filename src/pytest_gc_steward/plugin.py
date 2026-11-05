"""pytest plugin: steer the cyclic garbage collector and report what it finds.

Why this exists: objects caught in reference cycles are not freed by reference
counting; they wait for the cyclic GC. In long test sessions (test clients,
templates, tracebacks...) that garbage can pile up faster than the automatic
collector reclaims it, and every pytest-xdist worker bloats. The plugin can:

* ``gc.freeze()`` everything that exists before the first test (imported
  modules, collected items), so later collections don't rescan it;
* force ``gc.collect()`` every N tests and/or whenever RSS grew by N MB since
  the last forced collection;
* optionally disable the automatic collector for the session;
* report, per object type / test / module, what the collector reclaims, to
  find where the cycles come from.

All options are off by default: installing the plugin changes nothing until
you enable something.
"""

from __future__ import annotations

import gc
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from .rss import current_rss_mb

_KEY = "gc_steward"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("gc-steward", "garbage collector control and diagnostics")
    group.addoption(
        "--gc-freeze",
        action="store_true",
        default=None,
        help="gc.freeze() all objects alive before the first test, so later "
        "collections skip them (ini: gc_steward_freeze).",
    )
    group.addoption(
        "--gc-every",
        type=int,
        default=None,
        metavar="N",
        help="force gc.collect() after every N tests; 0 = off (ini: gc_steward_every).",
    )
    group.addoption(
        "--gc-rss-growth",
        type=float,
        default=None,
        metavar="MB",
        help="force gc.collect() when RSS grew by MB since the last forced "
        "collection; 0 = off (ini: gc_steward_rss_growth).",
    )
    group.addoption(
        "--gc-disable",
        action="store_true",
        default=None,
        help="disable the automatic collector during tests; combine with "
        "--gc-every / --gc-rss-growth (ini: gc_steward_disable).",
    )
    group.addoption(
        "--gc-report",
        default=None,
        metavar="PATH",
        help="collect after EVERY test with gc.DEBUG_SAVEALL and write a JSON "
        "report of what the GC reclaimed (by type, test and module). Slow: "
        "diagnostics only (ini: gc_steward_report).",
    )
    group.addoption(
        "--gc-report-top",
        type=int,
        default=None,
        metavar="N",
        help="how many entries to keep / show per report section "
        "(default 25, ini: gc_steward_report_top).",
    )
    parser.addini("gc_steward_freeze", "see --gc-freeze", type="bool", default=False)
    parser.addini("gc_steward_every", "see --gc-every", default="0")
    parser.addini("gc_steward_rss_growth", "see --gc-rss-growth", default="0")
    parser.addini("gc_steward_disable", "see --gc-disable", type="bool", default=False)
    parser.addini("gc_steward_report", "see --gc-report", default="")
    parser.addini("gc_steward_report_top", "see --gc-report-top", default="25")


def _opt(config: pytest.Config, name: str, ini: str, cast):
    value = config.getoption(name)
    if value is None:
        value = config.getini(ini)
    return cast(value) if value not in ("", None) else cast()


def _type_key(obj: object) -> str:
    cls = type(obj)
    module = cls.__dict__.get("__module__")
    if not isinstance(module, str) or module == "builtins":
        return cls.__qualname__
    return f"{module}.{cls.__qualname__}"


class Steward:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.freeze = bool(_opt(config, "gc_freeze", "gc_steward_freeze", bool))
        self.every = _opt(config, "gc_every", "gc_steward_every", int)
        self.rss_growth = _opt(config, "gc_rss_growth", "gc_steward_rss_growth", float)
        self.disable = bool(_opt(config, "gc_disable", "gc_steward_disable", bool))
        self.report_path = _opt(config, "gc_report", "gc_steward_report", str)
        self.top = _opt(config, "gc_report_top", "gc_steward_report_top", int) or 25
        self.active = bool(
            self.freeze or self.every or self.rss_growth or self.disable
        ) or bool(self.report_path)

        self.tests = 0
        self.started = False
        self.was_enabled = gc.isenabled()
        self.rss_start = current_rss_mb()
        self.rss_peak = self.rss_start or 0.0
        self.rss_baseline = self.rss_start
        self.frozen = 0
        self.forced = 0
        self.forced_seconds = 0.0
        self.forced_objects = 0
        self.auto = Counter()  # generation -> runs
        self.auto_seconds = 0.0
        self.auto_objects = 0
        self._auto_t0 = 0.0
        self._in_forced = False
        self.types: Counter[str] = Counter()
        self.per_test: Counter[str] = Counter()
        self.per_module: Counter[str] = Counter()

    # -- automatic collections (observed, not driven) -----------------------
    def _callback(self, phase: str, info: dict[str, Any]) -> None:
        if self._in_forced:
            return
        if phase == "start":
            self._auto_t0 = time.perf_counter()
        else:
            self.auto_seconds += time.perf_counter() - self._auto_t0
            self.auto[info.get("generation", -1)] += 1
            self.auto_objects += info.get("collected", 0)

    # -- forced collections --------------------------------------------------
    def _collect(self) -> int:
        self._in_forced = True
        t0 = time.perf_counter()
        try:
            collected = gc.collect()
        finally:
            self.forced_seconds += time.perf_counter() - t0
            self._in_forced = False
        self.forced += 1
        self.forced_objects += collected
        return collected

    def _census(self, nodeid: str) -> None:
        """Collect with DEBUG_SAVEALL and count what would have been freed."""
        old = gc.get_debug()
        gc.set_debug(old | gc.DEBUG_SAVEALL)
        try:
            self._collect()
        finally:
            gc.set_debug(old)
        found = Counter(_type_key(o) for o in gc.garbage)
        gc.garbage.clear()
        self._collect()  # now really free what DEBUG_SAVEALL kept alive
        total = sum(found.values())
        if total:
            self.types.update(found)
            self.per_test[nodeid] += total
            self.per_module[nodeid.split("::", 1)[0]] += total

    # -- hooks ---------------------------------------------------------------
    def start(self) -> None:
        self.started = True
        gc.callbacks.append(self._callback)
        if self.freeze:
            self._collect()
            gc.freeze()
            self.frozen = gc.get_freeze_count()
        if self.disable:
            gc.disable()
        self.rss_baseline = current_rss_mb()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem):
        if not self.started:
            self.start()
        yield
        self.tests += 1
        rss = current_rss_mb()
        if rss is not None:
            self.rss_peak = max(self.rss_peak, rss)
        if self.report_path:
            self._census(item.nodeid)
            self.rss_baseline = current_rss_mb()
            return
        due = bool(self.every) and self.tests % self.every == 0
        if not due and self.rss_growth and rss is not None and self.rss_baseline:
            due = rss - self.rss_baseline >= self.rss_growth
        if due:
            self._collect()
            self.rss_baseline = current_rss_mb()

    def stop(self) -> None:
        if self._callback in gc.callbacks:
            gc.callbacks.remove(self._callback)
        if self.freeze:
            gc.unfreeze()
        if self.disable and self.was_enabled:
            gc.enable()

    def stats(self) -> dict[str, Any]:
        return {
            "tests": self.tests,
            "frozen": self.frozen,
            "forced": self.forced,
            "forced_seconds": round(self.forced_seconds, 3),
            "forced_objects": self.forced_objects,
            "auto": {str(k): v for k, v in sorted(self.auto.items())},
            "auto_seconds": round(self.auto_seconds, 3),
            "auto_objects": self.auto_objects,
            "rss_start_mb": round(self.rss_start, 1) if self.rss_start else None,
            "rss_end_mb": _round(current_rss_mb()),
            "rss_peak_mb": round(self.rss_peak, 1) if self.rss_peak else None,
            "types": dict(self.types.most_common(self.top * 4)),
            "tests_top": dict(self.per_test.most_common(self.top * 4)),
            "modules": dict(self.per_module.most_common(self.top * 4)),
        }


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _is_xdist_controller(config: pytest.Config) -> bool:
    return config.pluginmanager.hasplugin("dsession") and not hasattr(
        config, "workerinput"
    )


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    steward = Steward(config)
    config.stash[_STASH] = steward
    config.stash[_WORKERS] = {}
    if not steward.active:
        return
    if not _is_xdist_controller(config):
        config.pluginmanager.register(steward, "gc-steward-runtime")
    if config.pluginmanager.hasplugin("xdist"):
        config.pluginmanager.register(_XdistController(config), "gc-steward-xdist")


def pytest_unconfigure(config: pytest.Config) -> None:
    steward = config.stash.get(_STASH, None)
    if steward is not None and steward.started:
        steward.stop()


def pytest_sessionfinish(session: pytest.Session) -> None:
    config = session.config
    steward = config.stash.get(_STASH, None)
    if steward is None or not steward.active:
        return
    if hasattr(config, "workerinput"):  # xdist worker: ship stats to controller
        config.workeroutput[_KEY] = steward.stats()


class _XdistController:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error) -> None:
        stats = getattr(node, "workeroutput", {}).get(_KEY)
        if stats:
            self.config.stash[_WORKERS][node.gateway.id] = stats


_STASH = pytest.StashKey[Steward]()
_WORKERS = pytest.StashKey[dict]()


def _merge(stats: list[dict[str, Any]]) -> dict[str, Counter]:
    merged = {"types": Counter(), "tests_top": Counter(), "modules": Counter()}
    for s in stats:
        for key in merged:
            merged[key].update(s.get(key, {}))
    return merged


def _line(name: str, s: dict[str, Any]) -> str:
    auto = ", ".join(f"gen{g} {n}" for g, n in s["auto"].items()) or "none"
    rss = "?"
    if s["rss_start_mb"] is not None:
        rss = f"{s['rss_start_mb']:.0f} -> {s['rss_end_mb']:.0f} MB"
        rss += f" (peak {s['rss_peak_mb']:.0f})"
    return (
        f"{name}: {s['tests']} tests, RSS {rss}; forced {s['forced']}x "
        f"({s['forced_seconds']:.2f} s, {s['forced_objects']} objects); "
        f"automatic {auto} ({s['auto_seconds']:.2f} s, "
        f"{s['auto_objects']} objects); frozen {s['frozen']}"
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config):
    steward = config.stash.get(_STASH, None)
    if steward is None or not steward.active or hasattr(config, "workerinput"):
        return
    workers = config.stash.get(_WORKERS, {})
    per_process = (
        dict(sorted(workers.items())) if workers else {"main": steward.stats()}
    )
    tr = terminalreporter
    tr.write_sep("=", "gc-steward")
    for name, s in per_process.items():
        tr.write_line(_line(name, s))
    if not steward.report_path:
        return
    merged = _merge(list(per_process.values()))
    top = steward.top
    for title, key in (
        ("objects reclaimed by the cyclic GC, by type", "types"),
        ("by test", "tests_top"),
        ("by module", "modules"),
    ):
        tr.write_line("")
        tr.write_line(f"{title} (top {top}):")
        for name, count in merged[key].most_common(top):
            tr.write_line(f"  {count:>10}  {name}")
    report = {
        "processes": per_process,
        "merged": {k: dict(v.most_common()) for k, v in merged.items()},
    }
    path = Path(steward.report_path)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    tr.write_line("")
    tr.write_line(f"gc-steward report written to {path}")
