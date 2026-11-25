"""Every mypy suppression in src/ names its error code and says why it is needed."""

import re

import pytest

from conftest import ROOT

IGNORE = re.compile(r"#\s*type:\s*ignore\b(?P<rest>.*)")
REASONED = re.compile(r"\[[a-z0-9-]+(?:,\s*[a-z0-9-]+)*\]\s+#\s*\S")


def _unreasoned(line: str) -> bool:
    found = IGNORE.search(line)
    return found is not None and not REASONED.match(found["rest"])


@pytest.mark.parametrize(
    "line, bad",
    [
        ("x = f()  # type: ignore[attr-defined]  # no stubs for f", False),
        ("x = f()  # type: ignore[arg-type, misc]  # a test double", False),
        ("x = f()  # type: ignore[attr-defined]", True),
        ("x = f()  # type: ignore  # no stubs for f", True),
        ("x = f()  # type: ignore", True),
        ("x = f()  # a plain comment", False),
    ],
)
def test_a_type_ignore_needs_a_code_and_a_reason(line, bad):
    assert _unreasoned(line) is bad


def test_every_type_ignore_in_src_has_a_code_and_a_reason():
    seen, bare = 0, []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            seen += IGNORE.search(line) is not None
            if _unreasoned(line):
                bare.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert seen, "found no `# type: ignore` in src/: the scan is looking in the wrong place"
    assert not bare, "a `# type: ignore[code]` needs a `# reason` after it:\n" + "\n".join(bare)
