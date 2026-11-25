"""The GitHub Pages deploy workflow stays bounded without racing the deploy action."""

import re

from conftest import ROOT

WORKFLOW = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")


def indented_block(header: str) -> str:
    """The lines under one YAML mapping key, indentation-aware and without a YAML dependency."""
    lines = WORKFLOW.splitlines()
    start = lines.index(header)
    indent = len(header) - len(header.lstrip())
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        end += 1
    return "\n".join(lines[start + 1 : end])


def test_deploy_job_has_a_ten_minute_timeout():
    deploy = indented_block("  deploy:")

    assert re.search(r"^    timeout-minutes: 10$", deploy, re.M)


def test_deploy_action_times_out_before_the_job_cap():
    deploy = indented_block("  deploy:")
    step = re.search(r"(?ms)^      - id: deployment\n(?P<body>(?:^        .*\n?)*)", deploy)
    assert step is not None

    assert re.search(r"^        uses: actions/deploy-pages@v5$", step.group("body"), re.M)
    assert re.search(r"^          timeout: ['\"]480000['\"]$", step.group("body"), re.M)
