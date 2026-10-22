"""Tests the Linux packages locally in throwaway Docker containers before pushing.

The same steps as .github/workflows/linux.yml, as far as they work in containers:

1. gst-plugins-rs (fallbacksrc, audioloudnorm) built against Ubuntu 24.04's
   GStreamer with the current Rust (cached image: once per branch)
2. the .deb and the .tar.gz (packaging/linux/build.sh) – exactly one of each
3. the .deb installed with apt in a clean Ubuntu 24.04, the self-test
4. the app as its service would run it, and the end-to-end checks: the API
   checks inside the container, the admin and viewer pages in the Windows Edge
5. a smoke test with switching, the purge, install.sh and --uninstall --purge

Not covered (CI only): systemd itself, WebRTC playback in the browser, Windows.
Everything stays in Docker: images lanstreamer-*, containers removed at the end.

    python tests/local_linux.py [--keep]    # --keep: the app stays at http://localhost:18180
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
HERE = REPO / "tests"
OUT = REPO / "build" / "local-linux"
RS_BRANCH = "0.13"  # keep in step with GST_PLUGINS_RS in linux.yml
RS_IMAGE = f"lanstreamer-rs:{RS_BRANCH}"
DEPS_IMAGE = "lanstreamer-deps:local"
TEST_IMAGE = "lanstreamer-debtest:local"
CONTAINER = "lanstreamer-debtest"
PORT = 18180
VERSION = "0.0.1-local"


def run(*cmd: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    print("  $", " ".join(cmd) if len(" ".join(cmd)) < 160 else " ".join(cmd)[:157] + "...")
    return subprocess.run(cmd, check=check, **kw)


def docker(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return run("docker", *args, check=check, **kw)


def in_container(*cmd: str) -> int:
    return docker("exec", CONTAINER, *cmd, check=False).returncode


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def build_packages() -> Path:
    step(f"gst-plugins-rs {RS_BRANCH} (cached Docker image)")
    docker("build", "-q", "-t", RS_IMAGE, "--build-arg", f"BRANCH={RS_BRANCH}",
           "-f", str(HERE / "docker" / "rs-plugins.Dockerfile"), str(HERE / "docker"))  # fmt: skip
    rs = OUT / "rsplugins"
    shutil.rmtree(OUT, ignore_errors=True)
    rs.mkdir(parents=True)
    cid = docker("create", RS_IMAGE, capture_output=True, text=True).stdout.strip()
    try:
        docker("cp", f"{cid}:/out/.", str(rs))
    finally:
        docker("rm", cid, capture_output=True)

    step("packages (packaging/linux/build.sh)")
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}  # pip of this venv
    run(str(REPO / "packaging" / "linux" / "build.sh"), VERSION, str(rs), str(OUT / "pkg"), env=env)
    debs = list((OUT / "pkg").glob("lan-streamer_*.deb"))
    tars = list((OUT / "pkg").glob("lan-streamer-*.tar.gz"))
    others = [p.name for p in (OUT / "pkg").glob("*.tar.gz") if p not in tars]
    if len(debs) != 1 or len(tars) != 1 or others:
        raise SystemExit(f"FAILED: expected one .deb and one .tar.gz, got {debs} {tars} (other archives: {others})")
    print(f"  ok: {debs[0].name}, {tars[0].name}")

    context = OUT / "context"
    context.mkdir()
    shutil.copy(debs[0], context)
    shutil.copy(REPO / "packaging" / "linux" / "depends.txt", context)
    shutil.copy(HERE / "docker" / "start.sh", context)
    step("clean Ubuntu 24.04 + apt install ./lan-streamer_….deb")
    dockerfile = str(HERE / "docker" / "deb-test.Dockerfile")
    docker("build", "-q", "--target", "deps", "-t", DEPS_IMAGE, "-f", dockerfile, str(context))
    docker("build", "-q", "-t", TEST_IMAGE, "-f", dockerfile, str(context))
    return OUT / "pkg"


def wait_running(base: str, timeout_s: float = 180) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/api/status", timeout=2).json()["state"] == "running":
                return
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(1)
    docker("logs", "--tail", "40", CONTAINER, check=False)
    raise SystemExit("FAILED: lan-streamer did not start in the container")


def e2e(results: dict[str, int]) -> None:
    base = f"http://localhost:{PORT}"
    step("self-test in the container")
    results["self-test"] = in_container("lan-streamer", "selftest")

    step("app in the container (as user lanstreamer, data in /var/lib/lan-streamer)")
    wait_running(base)
    print(f"  ok: {base}")
    py, checks = "/e2e/bin/python", "/src/tests/e2e"
    step("web API (in the container)")
    results["web API"] = in_container(py, f"{checks}/check_api.py", "http://localhost:8080")

    sys.path.insert(0, str(HERE / "e2e"))
    import edge

    if edge.available():
        with edge.windows_edge() as ws:
            step("admin page in Edge")
            results["admin page"] = run(sys.executable, str(HERE / "e2e" / "check_admin.py"), base,
                                        "--cdp", ws, check=False).returncode  # fmt: skip
            step("viewer side in Edge (real yt-dlp, the clip from the container's web server)")
            results["viewer side"] = run(sys.executable, str(HERE / "e2e" / "check_viewer.py"), base,
                                         "--cdp", ws, "--link", "http://127.0.0.1:8000/clip.mp4",
                                         check=False).returncode  # fmt: skip
    else:
        print("  skipped admin and viewer pages: no Windows Edge reachable from here")

    step("separate picture and sound, ingest, restream (in the container)")
    results["split source"] = in_container(py, f"{checks}/check_split_source.py", "http://localhost:8080",
                                           "http://127.0.0.1:8000/clip.mp4")  # fmt: skip
    results["ingest"] = in_container(py, f"{checks}/check_ingest.py", "http://localhost:8080",
                                     "/opt/lan-streamer/venv/bin/python")  # fmt: skip
    results["restream"] = in_container(py, f"{checks}/check_restream.py", "http://localhost:8080")
    if "viewer side" in results:
        results["lower third"] = in_container(
            "grep", "-m1", "Overlay shown: .*submitted by CI viewer", "/var/lib/lan-streamer/logs/output.log")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="leave the app container running")
    args = parser.parse_args()
    results: dict[str, int] = {}
    pkg = build_packages()
    docker("rm", "-f", CONTAINER, capture_output=True, check=False)
    docker("run", "-d", "--name", CONTAINER, "-p", f"{PORT}:8080",
           "-v", f"{HERE}:/src/tests:ro", TEST_IMAGE, capture_output=True)  # fmt: skip
    try:
        e2e(results)
    finally:
        if args.keep:
            print(f"\nStill running: http://localhost:{PORT}/admin (password ci-password-123);"
                  f" stop with: docker rm -f {CONTAINER}")  # fmt: skip
        else:
            with open(OUT / "app.log", "w") as log:
                docker("logs", CONTAINER, stdout=log, stderr=subprocess.STDOUT, check=False)
            docker("rm", "-f", CONTAINER, capture_output=True, check=False)

    step("smoke test with switching (fresh container)")
    results["smoke test"] = docker("run", "--rm", TEST_IMAGE, "/start.sh", "smoke", check=False).returncode

    step("apt purge (fresh container)")
    results["purge"] = docker("run", "--rm", TEST_IMAGE, "sh", "-c",
        "apt-get purge -y -q lan-streamer >/dev/null && ! id lanstreamer 2>/dev/null"
        " && for d in /opt/lan-streamer /var/lib/lan-streamer /usr/bin/lan-streamer; do"
        " if [ -e $d ]; then echo \"left behind: $d\"; exit 1; fi; done && echo 'OK: purged'",
        check=False).returncode  # fmt: skip

    step("install.sh and install.sh --uninstall --purge (the .tar.gz)")
    results["install.sh"] = docker("run", "--rm", "-v", f"{pkg}:/pkg:ro", DEPS_IMAGE, "sh", "-c",
        "set -e; tar -xzf /pkg/lan-streamer-*.tar.gz -C /tmp; /tmp/lan-streamer-*/install.sh >/dev/null;"
        " lan-streamer selftest >/dev/null; /tmp/lan-streamer-*/install.sh --uninstall --purge;"
        " for p in /opt/lan-streamer /usr/local/bin/lan-streamer; do if [ -e $p ]; then"
        " echo \"left behind: $p\"; exit 1; fi; done; if id lanstreamer 2>/dev/null; then exit 1; fi; echo 'OK: install.sh'",
        check=False).returncode  # fmt: skip

    print()
    for name, code in results.items():
        print(f"{'OK    ' if code == 0 else 'FAILED'} {name}")
    if any(results.values()):
        print(f"app log: {OUT / 'app.log'}")
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
