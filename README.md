# pytest-gc-steward

**Stop pytest workers from bloating.** A pytest plugin that takes charge of
Python's cyclic garbage collector during a test session — `gc.freeze()` at
startup, collections every N tests or after N MB of growth — and tells you
**what** the collector keeps reclaiming, so you can fix the source.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-%E2%98%95-yellow)](https://buymeacoffee.com/mpasternak)

```bash
pip install pytest-gc-steward
pytest --gc-freeze --gc-every 25        # keep memory flat
pytest --gc-report gc.json              # find out where the garbage comes from
```

---

## Why would I need this?

Most Python objects die the moment their reference count drops to zero. Objects
caught in a **reference cycle** (A → B → A) never get there — they wait for the
cyclic garbage collector. Test suites produce a lot of them: test-client
responses, rendered templates and their contexts, tracebacks holding frames
holding every local variable...

If the collector reclaims that garbage more slowly than your tests produce it,
**each process quietly grows** for the whole session. With `pytest-xdist` you
pay that once per worker.

### A real case

This plugin was written while debugging a Django project with ~11 000 tests.
`pytest -n auto` (14 workers) on a 14 GB laptop froze the machine: swap
thrashing, hard reset. What we found:

| | time | RSS of one worker after 1266 tests |
|---|---|---|
| before | 247 s | **1801 MB** |
| `gc.collect()` after *every* test | 719 s | 581 MB |
| `--gc-freeze --gc-every 25` | **239 s** | **624 MB** |

`--gc-report` then showed *why*: about 85% of what the collector reclaimed was
compiled template nodes. Three things had stacked up:

1. **Python 3.14.0–3.14.4** shipped an incremental GC that, on large heaps,
   collected cycles far too slowly. It was
   [reverted in 3.14.5](https://discuss.python.org/t/reverting-the-incremental-gc-in-python-3-14-and-3-15/107014).
2. The project's test settings used **uncached template loaders**, so every
   render re-parsed every template.
3. **Django's test client** puts every response in a reference cycle
   (`response.json = partial(self._parse_json, response)`), and the response
   carries all rendered templates and contexts with it.

Fixing the source (cached loaders, a current Python) brought the worker to the
same 625 MB **without** forced collections. The plugin pointed at all three;
the reproducer for the third is being reported upstream.

### When it helps

- Memory of test processes grows steadily over a session, and `gc.collect()`
  makes it drop. That's the signature of cyclic garbage, not of a real leak.
- You run many xdist workers and RAM, not CPU, is what limits `-n`.
- You are stuck on a Python with lazy cycle collection (e.g. 3.14.0–3.14.4).
- You want to *find* which tests and which object types produce cycles.

If `gc.collect()` does **not** reduce memory, you have objects that are still
referenced (caches, globals, fixtures): a real leak. This plugin won't fix that,
although the report still shows you what isn't garbage.

---

## Usage

Everything is **off by default**. Installing the plugin changes nothing until
you turn something on, from the command line or `pytest.ini` /
`pyproject.toml`:

| Option | ini | What it does |
|---|---|---|
| `--gc-freeze` | `gc_steward_freeze = true` | Before the first test: `gc.collect()` + `gc.freeze()`. Everything imported and collected so far (often millions of objects in a Django project) moves to the permanent generation, so later collections don't rescan it. This is what makes frequent collections cheap. |
| `--gc-every N` | `gc_steward_every = N` | Force `gc.collect()` after every N tests. |
| `--gc-rss-growth MB` | `gc_steward_rss_growth = MB` | Force `gc.collect()` when the process grew by MB since the last forced collection. The baseline resets after each collection, because CPython rarely gives memory back to the OS. |
| `--gc-disable` | `gc_steward_disable = true` | Turn the automatic collector off during tests (setup before the first test still runs with it on). Use together with `--gc-every` / `--gc-rss-growth`, or you may be the one who freezes the laptop. |
| `--gc-report PATH` | `gc_steward_report = PATH` | Diagnostics: after **every** test collect with `gc.DEBUG_SAVEALL`, count what was reclaimed by object type, test and module, print the top entries and write a JSON report. Slow — run it once, on a subset. |
| `--gc-report-top N` | `gc_steward_report_top = N` | Entries per report section (default 25). |

Command-line options win over ini values.

### Recommended setup

```ini
# pytest.ini
[pytest]
gc_steward_freeze = true
gc_steward_every = 25
```

`--gc-every 25` with `--gc-freeze` cost us nothing measurable; the run was even
slightly faster, because the process wasn't dragging a 1.8 GB heap around.
Without `--gc-freeze`, each full collection rescans the whole heap — in our
project ~0.4 s each.

### pytest-xdist

Each worker steers its own collector. The controller gathers the numbers and
prints one line per worker, and `--gc-report` merges all workers into one file:

```
============================== gc-steward ==============================
gw0: 412 tests, RSS 351 -> 598 MB (peak 640); forced 16x (1.84 s, 1203391 objects); automatic gen0 1832, gen1 166, gen2 3 (0.91 s, 88213 objects); frozen 2154007
gw1: 397 tests, RSS 350 -> 571 MB (peak 612); forced 15x (1.71 s, 1176502 objects); automatic gen0 1790, gen1 162, gen2 2 (0.88 s, 84117 objects); frozen 2154011
```

### Reading a report

```bash
pytest tests/admin --gc-report gc.json -p no:randomly
```

```
objects reclaimed by the cyclic GC, by type (top 25):
     2132705  django.template.base.Token
     1201038  django.template.base.TextNode
      988340  django.template.base.FilterExpression
      ...
by test (top 25):
      281339  tests/admin/test_home_links.py::test_admin_home_links_resolve
      ...
```

- **Types** say *what* is in the cycles. Template nodes point at template
  compilation; `frame` / `traceback` point at stored exceptions; your own
  classes point at your own code.
- **Tests / modules** say *where*. A handful of tests standing out is a
  local bug; an even spread points at shared infrastructure (a test client,
  a base class, middleware).
- To find the cycle itself, take one suspect object and walk
  `gc.get_referents()` until you are back where you started. Usually it's one
  attribute pointing back at its owner (`self.callback = partial(f, self)`).

The JSON file holds per-process stats (`processes`) and merged counters
(`merged.types`, `merged.tests_top`, `merged.modules`).

### Terminal line, explained

`forced` are the collections the plugin ran; `automatic` are the interpreter's
own, per generation, observed through `gc.callbacks`. `objects` is what each
kind reclaimed. If `forced` reclaims a lot while `automatic gen2` stays near
zero, the interpreter wasn't getting around to your cycles.

---

## How it works

Hooks around `pytest_runtest_protocol`: the first call optionally collects and
freezes; after each test it decides whether to collect (counter or RSS
growth). RSS comes from `/proc/self/statm` on Linux, `psutil` elsewhere if it is
installed (`pip install pytest-gc-steward[rss]`), and peak RSS from
`resource.getrusage` as a last resort. At session end the freeze is undone and
the automatic collector re-enabled.

Requires Python ≥ 3.10 and pytest ≥ 7.

## Support

If this saved your laptop (or your CI bill), you can
[buy me a coffee](https://buymeacoffee.com/mpasternak) ☕.

Issues and pull requests: https://github.com/iplweb/pytest-gc-steward

## License

MIT
