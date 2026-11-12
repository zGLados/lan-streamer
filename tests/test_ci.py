"""The CI workflows: what decides which checks run must not be able to skip them by mistake."""

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_pull_requests_only_run_without_write_rights():
    flow = yaml.safe_load((WORKFLOWS / "tests.yml").read_text(encoding="utf-8"))
    assert list(flow[True]) == ["pull_request"]  # YAML reads the key "on" as True
    assert flow["permissions"] == {"contents": "read"}


def test_scope_outputs_come_from_its_own_step():
    # 2026-10-26: the job's outputs read needs.scope… (empty) – every check was skipped
    for file in ("windows.yml", "linux.yml"):
        scope = yaml.safe_load((WORKFLOWS / file).read_text(encoding="utf-8"))["jobs"]["scope"]
        assert scope["outputs"], file
        for name, value in scope["outputs"].items():
            assert value == f"${{{{ steps.scope.outputs.{name} }}}}", (file, name, value)


def test_checks_run_unless_scope_says_false():
    # an empty answer must mean "check", never "skip"
    for file in ("windows.yml", "linux.yml"):
        text = (WORKFLOWS / file).read_text(encoding="utf-8")
        assert not re.search(r"needs\.scope\.outputs\.(full|update) == 'true'", text), file


def _caches(flow: dict) -> set[tuple[str, str]]:
    """(path, key) of every actions/cache step."""
    steps = [step for job in flow["jobs"].values() for step in job.get("steps", [])]
    return {(s["with"]["path"], s["with"]["key"]) for s in steps if str(s.get("uses", "")).startswith("actions/cache@")}


def test_caches_yml_fills_what_the_builds_read():
    # a tag run only finds main's caches; caches.yml keeps them there (2026-11-12)
    caches = yaml.safe_load((WORKFLOWS / "caches.yml").read_text(encoding="utf-8"))
    for file in ("windows.yml", "linux.yml"):
        flow = yaml.safe_load((WORKFLOWS / file).read_text(encoding="utf-8"))
        for name in ("GST_VERSION", "PYGOBJECT_VERSION", "GST_ROOT", "GST_PLUGINS_RS"):
            if name in flow["env"]:
                assert caches["env"].get(name) == flow["env"][name], (file, name)
        wanted = {c for c in _caches(flow) if not c[1].startswith("apt-")}  # apt: per run
        assert wanted, file
        assert wanted <= _caches(caches), (file, wanted - _caches(caches))
