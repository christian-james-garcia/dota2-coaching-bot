#!/usr/bin/env python3
"""Run the test suite without pytest.

The tests are plain functions with plain asserts, so `pytest` runs them too.
This runner exists because the project's own dependencies are stubbed in tests
and pytest is not always installable, and a suite nobody can run is no suite.

    python3 run_tests.py            # everything
    python3 run_tests.py queue      # only files matching "queue"
"""
import importlib.util
import os
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent
TESTS = ROOT / "tests"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def main(argv):
    sys.path.insert(0, str(TESTS))
    pattern = argv[0] if argv else ""
    files = sorted(p for p in TESTS.glob("test_*.py") if pattern in p.name)
    if not files:
        print(f"no test files match {pattern!r}")
        return 1

    passed, failures = 0, []
    for path in files:
        module = load(path)
        names = sorted(n for n in dir(module) if n.startswith("test_"))
        print(f"\n{path.name}")
        for name in names:
            try:
                getattr(module, name)()
            except Exception:
                failures.append((path.name, name, traceback.format_exc()))
                print(f"  FAIL  {name}")
            else:
                passed += 1
                print(f"  ok    {name}")

    print(f"\n{'-' * 60}")
    for filename, name, tb in failures:
        print(f"\nFAILED {filename}::{name}\n{tb}")
    print(f"{passed} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
