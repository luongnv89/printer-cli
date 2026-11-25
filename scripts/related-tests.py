"""scripts/related-tests.py: run the unit tests related to the files pre-commit passes.

pre-commit passes the staged files on commit, and the PR diff's files in the CI
guard's --from-ref/--to-ref run. A changed test file runs itself; a changed module
foo.py (or pkg/__init__.py) runs test_foo.py and foo_test.py (test_pkg.py) under the
test directory; a changed conftest.py runs the whole directory. Nothing related
exits 0, because the push-stage full suite is the gate.
"""

import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
tests = Path("tests")
if args[:1] == ["--tests"]:
    tests, args = Path(args[1]), args[2:]
if not tests.is_dir():
    sys.exit(f"related-tests: {tests} not found; pass --tests <the unit test directory>")

selected = set()
for f in map(Path, args):
    is_test = f.name.startswith("test_") or f.stem.endswith("_test")
    if f.name == "conftest.py":
        selected.add(str(tests))
    elif is_test and tests in f.parents:
        selected.add(str(f))
    else:
        stem = f.parent.name if f.stem == "__init__" else f.stem
        for pattern in (f"test_{stem}.py", f"{stem}_test.py"):
            selected.update(str(t) for t in tests.rglob(pattern))

if not selected:
    print("related-tests: no unit test matches these files; pre-push runs them all.")
    sys.exit(0)
# -n auto (pytest-xdist): a cli.py change selects tests/test_cli.py, 13 s serial and
# 3 s on 4 workers (measured 2026-11-25); keep the commit stage under 10 s.
code = subprocess.call([sys.executable, "-m", "pytest", "-x", "-q", "-n", "auto", *sorted(selected)])
sys.exit(0 if code == 5 else code)  # 5: the selected files collected no test
