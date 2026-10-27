"""Stage reviewed runtime source and locked caches; never copy user state."""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_TREES = {"zcode_gateway", "generator", "mail-worker", "assets", "tools", "docs"}
ROOT_FILES = {"LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "config.example.yaml",
              "requirements-linux.txt"}
PRIVATE_PARTS = {".git", ".claude", ".zcode", "graphify-out", "__pycache__", "node_modules",
                 "accounts", "accounts-parked", "accounts_parked", "logs", "log", "output",
                 "backups", "backup", "data", "state", "cache", ".wrangler", "captures", "flows",
                 "generator-data"}
PRIVATE_NAMES = {"config.yaml", "mail.json", "gmail.json", "login.txt", "pool.json",
                 "proxies.txt", "credentials.json", "ip_country.json"}
PRIVATE_PATTERNS = ("*.pyc", "*.pyo", "*.exe", "*.dll", "*.db*", "*.sqlite*", "*.log*",
                    "*.har", "*.out", "*.err", "*.pid", "*.pem", "*.key", ".env*",
                    ".pw-*", "config.yaml.*", "config.local.*", "credentials.json.*",
                    "mail.json.*", "gmail.json.*", "*.bak", "*.backup", "*~",
                    "VERIFICATION*.md", "WINDOWS-MIGRATION*.md", "BUG-AUDIT-*.md")


def eligible_source(relative: str) -> bool:
    path = Path(relative)
    if any(part in PRIVATE_PARTS or part.startswith((".venv", ".pw-")) for part in path.parts):
        return False
    if path.name in PRIVATE_NAMES or any(fnmatch.fnmatch(path.name, p) for p in PRIVATE_PATTERNS):
        return False
    return (relative in ROOT_FILES or path.parts[0] in SOURCE_TREES or
            path.parts[:2] == ("packaging", "fedora"))


def copy_sources(root: Path, stage: Path) -> list[str]:
    # Include uncommitted Linux port files while honoring source-control ignores.
    # Tracked files are also filtered: .gitignore alone cannot hide tracked state.
    listing = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    sources = sorted({p.decode() for p in listing.split(b"\0") if p and eligible_source(p.decode())})
    for name in sources:
        source = root / name
        if source.is_symlink():
            raise ValueError("Source symlinks are not permitted: " + name)
        if not source.is_file():
            raise ValueError("Listed source is missing: " + name)
        if source.suffix in {".py", ".js", ".ts", ".html", ".sh", ".toml", ".yaml", ".json"}:
            if re.search(rb"(?m)^(<<<<<<< |>>>>>>> )", source.read_bytes()):
                raise ValueError("Unresolved merge marker in: " + name)
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return sources


def check_symlinks(stage: Path) -> None:
    for path in stage.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(stage.resolve()):
            raise ValueError("External runtime symlink: " + str(path.relative_to(stage)))


def normalize_permissions(stage: Path) -> None:
    """Public package resources must remain readable regardless of builder umask."""
    stage.chmod(0o755)
    for path in stage.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def stage_runtime(root: Path, stage: Path, python_root: Path) -> None:
    from version import read_version
    expected = root / "build/fedora" / f"autozcoder-{read_version(root)}-fedora"
    if stage != expected or stage.is_symlink():
        raise ValueError("Refusing unexpected staging path")
    patcher = os.environ.get("PATCHELF") or shutil.which("patchelf")
    if not patcher:
        raise ValueError("patchelf is required for the private CPython runtime")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    sources = copy_sources(root, stage)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".links")
    for name in ["python", "generator-packages", "browsers", "node-tools"]:
        shutil.copytree(root / "build/fedora" / name, stage / name, symlinks=True, ignore=ignore)
    shutil.copytree(python_root, stage / "cpython", symlinks=True, ignore=ignore)
    check_symlinks(stage)
    # Relocate console entrypoints. No build-machine interpreter is required.
    for directory, interpreter in [("generator-packages/bin", "/usr/lib64/autozcoder/generator-python"),
                                   ("python/bin", "/usr/bin/python3")]:
        for script in (stage / directory).glob("*"):
            if script.is_file() and not script.is_symlink():
                raw = script.read_bytes()
                if raw.startswith(b"#!"):
                    script.write_bytes(("#!" + interpreter + "\n").encode() + raw.split(b"\n", 1)[1])
    for directory in ["cpython", "generator-packages", "python", "generator", "zcode_gateway"]:
        for source in (stage / directory).rglob("*.py"):
            source.chmod(0o644)
    # Remove upstream build-machine paths from private Tcl, without mutating its cache.
    for library in (stage / "cpython/lib").glob("libtcl*.so"):
        subprocess.run([patcher, "--set-rpath", "$ORIGIN", str(library)], check=True)
    for name in ["generator-python", "wrangler-run"]:
        shutil.copy2(root / "packaging/fedora" / name, stage / name)
        (stage / name).chmod(0o755)
    required = ["generator/main.py", "generator/config.py", "generator/fingerprint.py",
                "generator/proxy_geo.py", "mail-worker/src/index.js", "config.example.yaml",
                "tools/captcha-mint/LICENSE.vendor", "THIRD_PARTY_NOTICES.md",
                "cpython/bin/python3.12", "generator-packages/playwright/driver/node",
                "node-tools/node_modules/wrangler/bin/wrangler.js",
                "browsers/chromium-1248/chrome-linux64/chrome"]
    for name in required:
        if not (stage / name).is_file():
            raise ValueError("Required runtime asset missing: " + name)
    # Executable Windows payloads are never shipped, even if a cache contains one.
    for pattern in ("*.exe", "*.dll"):
        for path in stage.rglob(pattern):
            path.unlink()
    with (stage / "BUNDLED-LICENSES.txt").open("w", encoding="utf-8") as out:
        out.write((stage / "THIRD_PARTY_NOTICES.md").read_text())
        out.write("\n--- tools/captcha-mint/LICENSE.vendor ---\n")
        out.write((stage / "tools/captcha-mint/LICENSE.vendor").read_text())
        for folder in ["python", "generator-packages"]:
            for dist in sorted(metadata.distributions(path=[str(stage / folder)]),
                               key=lambda d: d.metadata["Name"].lower()):
                out.write(f"\n{'=' * 72}\n{folder}: {dist.metadata['Name']} {dist.version}\n")
                out.write("License: " + str(dist.metadata.get("License-Expression") or
                                           dist.metadata.get("License") or "see bundled license files") + "\n")
                for f in sorted(dist.files or [], key=str):
                    if any(word in str(f).lower() for word in ["license", "copying", "notice"]):
                        p = dist.locate_file(f)
                        if p.is_file():
                            out.write(f"\n--- {f} ---\n" + p.read_text(errors="replace") + "\n")
        for folder in ["cpython", "browsers", "node-tools"]:
            for p in sorted((stage / folder).rglob("*")):
                if p.is_file() and not p.is_symlink() and any(word in p.name.lower() for word in ["license", "copying", "notice"]):
                    out.write(f"\n--- {p.relative_to(stage)} ---\n" + p.read_text(errors="replace") + "\n")
    (stage / "SOURCE-FILES.txt").write_text("\n".join(sources) + "\n")
    manifest = {}
    for p in sorted(stage.rglob("*")):
        name = str(p.relative_to(stage))
        if p.is_symlink():
            manifest[name] = {"symlink": os.readlink(p)}
        elif p.is_file():
            with p.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest[name] = {"size": p.stat().st_size, "sha256": digest}
    (stage / "RUNTIME-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    normalize_permissions(stage)
    print(f"Staged {len(sources)} reviewed source files; {len(manifest)} total runtime entries")


if __name__ == "__main__":
    root, stage, python_root = (Path(p).absolute() for p in sys.argv[1:])
    stage_runtime(root, stage, python_root)
