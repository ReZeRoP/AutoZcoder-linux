#!/usr/bin/env python3
"""Validate version-locked caches without importing optional runtime modules."""
from __future__ import annotations

import importlib.metadata as metadata
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/fedora"
PIN = json.loads((ROOT / "packaging/fedora/runtime-versions.json").read_text())


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_python_cache(folder: Path, lock: Path) -> None:
    expected = {}
    for line in lock.read_text().splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", line)
        if not match:
            raise ValueError(f"Unsupported lock entry: {line!r}")
        expected[normalized(match[1])] = match[2]
    found = {normalized(d.metadata["Name"]): d.version
             for d in metadata.distributions(path=[str(folder)])}
    if found != expected:
        raise ValueError(f"{folder}: lock mismatch; expected {expected}, found {found}")


def check_node_cache(tools: Path, expected: Path) -> None:
    for name in ["package.json", "package-lock.json"]:
        if (tools / name).read_bytes() != (expected / name).read_bytes():
            raise ValueError(f"Node cache {name} differs from committed lock")
    packages = json.loads((expected / "package-lock.json").read_text())["packages"]
    for relative, entry in packages.items():
        if not relative:
            continue
        manifest = tools / relative / "package.json"
        if not manifest.is_file() and entry.get("optional"):
            continue  # npm omits optional binaries for other platforms.
        if not manifest.is_file():
            raise ValueError(f"Missing locked Node package: {relative}")
        installed = json.loads(manifest.read_text())
        if installed.get("version") != entry.get("version"):
            raise ValueError(f"Node package version differs from lock: {relative}")


def check_caches(python_root: Path) -> None:
    for folder, lock in [("python", "requirements.lock"),
                         ("generator-packages", "generator-requirements.lock")]:
        check_python_cache(BUILD / folder, ROOT / "packaging/fedora" / lock)
    actual = subprocess.check_output(
        [str(python_root / "bin/python3.12"), "-I", "-c",
         "import platform; print(platform.python_version()); print(platform.machine())"],
        text=True).splitlines()
    if actual != [PIN["private_python"], PIN["architecture"]]:
        raise ValueError(f"Unexpected private Python: {actual}")
    if sys.version_info[:2] != tuple(map(int, PIN["system_python"].split("."))):
        raise ValueError("Run packaging with Fedora's Python " + PIN["system_python"])
    tools = BUILD / "node-tools"
    check_node_cache(tools, ROOT / "packaging/fedora/node-tools")
    installed = json.loads((tools / "node_modules/wrangler/package.json").read_text())
    if installed["version"] != PIN["wrangler"]:
        raise ValueError("Wrong Wrangler version")
    browser_spec = json.loads((BUILD / "generator-packages/playwright/driver/package/browsers.json").read_text())
    for name, key in [("chromium", "chromium_revision"), ("ffmpeg", "ffmpeg_revision")]:
        entry = next(b for b in browser_spec["browsers"] if b["name"] == name)
        if entry["revision"] != PIN[key]:
            raise ValueError(f"Playwright's {name} revision changed")
    for filename in [f"chromium-{PIN['chromium_revision']}/chrome-linux64/chrome",
                     f"ffmpeg-{PIN['ffmpeg_revision']}/ffmpeg-linux"]:
        if not (BUILD / "browsers" / filename).is_file():
            raise ValueError("Missing browser asset: " + filename)
    print("Locked Python, private CPython, Wrangler and browser caches verified")


if __name__ == "__main__":
    if sys.argv[1:] == ["--node-only"]:
        check_node_cache(BUILD / "node-tools", ROOT / "packaging/fedora/node-tools")
    else:
        check_caches(Path(sys.argv[1]).resolve())
