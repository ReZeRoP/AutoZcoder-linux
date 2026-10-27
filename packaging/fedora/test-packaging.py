#!/usr/bin/env python3
"""Offline packaging regression tests; never import or start the application."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from version import ROOT, read_version, stage_path

spec = importlib.util.spec_from_file_location("staging", Path(__file__).with_name("stage-runtime.py"))
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)
(ROOT / "build").mkdir(exist_ok=True)


class PackagingTests(unittest.TestCase):
    def temporary(self):
        result = tempfile.TemporaryDirectory(prefix="packaging-test-", dir=ROOT / "build")
        self.addCleanup(result.cleanup)
        return Path(result.name)

    def test_version_matches_spec(self):
        rpm_version = subprocess.check_output(
            ["rpmspec", "--query", "--srpm", "--queryformat", "%{VERSION}",
             str(ROOT / "packaging/fedora/autozcoder.spec")], text=True)
        self.assertEqual(read_version(), rpm_version)
        self.assertEqual(stage_path().name, f"autozcoder-{rpm_version}-fedora")

    def test_version_does_not_import_application(self):
        root = self.temporary()
        (root / "zcode_gateway").mkdir()
        (root / "zcode_gateway/__init__.py").write_text('__version__ = "9.8.7"\nraise RuntimeError("never import")\n')
        self.assertEqual(read_version(root), "9.8.7")

    def test_version_rejects_nonliteral(self):
        root = self.temporary()
        (root / "zcode_gateway").mkdir()
        (root / "zcode_gateway/__init__.py").write_text('__version__ = str(123)\n')
        with self.assertRaises(ValueError):
            read_version(root)

    def test_ordinary_helpers_and_licenses_retained(self):
        for filename in ["generator/fingerprint.py", "generator/proxy_geo.py",
                         "generator/config.py", "generator/requirements.txt",
                         "zcode_gateway/dashboard/index.html", "mail-worker/wrangler.toml",
                         "tools/captcha-mint/captcha-solver.ts", "tools/captcha-mint/LICENSE.vendor",
                         "docs/UPSTREAM-README.md", "THIRD_PARTY_NOTICES.md",
                         "config.example.yaml", "packaging/fedora/autozcoder.spec"]:
            with self.subTest(filename=filename):
                self.assertTrue(staging.eligible_source(filename))

    def test_private_and_windows_files_excluded(self):
        for filename in ["config.yaml", ".claude/settings.json", ".zcode/v2/credentials.json",
                         "graphify-out/graph.json", "generator/accounts-parked/test.json",
                         "generator/backups/config.yaml", "generator/gateway.db-wal",
                         "generator/.env.local", "generator/mail.json", "generator/gmail.json",
                         "generator/output/a.txt", "generator/logs/session.txt",
                         "generator/.pw-profile/Preferences", "generator/private.key",
                         "docs/VERIFICATION.md", "docs/WINDOWS-MIGRATION.md",
                         "tools/captcha-mint/captcha-mint.exe", "assets/native.dll",
                         "tools/captcha-mint/node_modules/foo/package.json"]:
            with self.subTest(filename=filename):
                self.assertFalse(staging.eligible_source(filename))

    def test_copy_sources_filters_tracked_private_data(self):
        root = self.temporary()
        target = self.temporary()
        (root / "generator").mkdir()
        (root / "generator/main.py").write_text("x = 1\n")
        (root / "generator/mail.json").write_text('{"private":true}')
        with patch.object(staging.subprocess, "check_output", return_value=b"generator/main.py\0generator/mail.json\0"):
            self.assertEqual(staging.copy_sources(root, target), ["generator/main.py"])
        self.assertFalse((target / "generator/mail.json").exists())

    def test_merge_markers_rejected(self):
        root = self.temporary()
        target = self.temporary()
        (root / "generator").mkdir()
        (root / "generator/main.py").write_text("<<<<<<< HEAD\nx=1\n=======\nx=2\n>>>>>>> change\n")
        with patch.object(staging.subprocess, "check_output", return_value=b"generator/main.py\0"):
            with self.assertRaisesRegex(ValueError, "merge marker"):
                staging.copy_sources(root, target)

    def test_external_symlink_rejected(self):
        root = self.temporary()
        (root / "bad-link").symlink_to(ROOT / "README.md")
        with self.assertRaisesRegex(ValueError, "External runtime symlink"):
            staging.check_symlinks(root)

    def test_internal_symlink_allowed(self):
        root = self.temporary()
        (root / "module").write_text("content")
        (root / "link").symlink_to("module")
        staging.check_symlinks(root)

    def test_node_manifest_exact_wrangler(self):
        root = ROOT / "packaging/fedora/node-tools"
        package = json.loads((root / "package.json").read_text())
        lock = json.loads((root / "package-lock.json").read_text())
        self.assertEqual(package["dependencies"]["wrangler"], "4.151.0")
        self.assertEqual(lock["packages"]["node_modules/wrangler"]["version"], "4.151.0")
        self.assertTrue(lock["packages"]["node_modules/wrangler"]["integrity"].startswith("sha512-"))

    def test_gitignore_keeps_spec_and_ignores_state(self):
        ignored = [".claude/test", "graphify-out/graph.json", ".zcode/credentials.json",
                   "accounts-parked/test.json", "backups/config.yaml", "generator/gateway.db-shm",
                   "build/private", "tools/captcha-mint/captcha-mint.exe"]
        for name in ignored:
            result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "-q", name])
            self.assertEqual(result.returncode, 0, name)
        result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "-q",
                                 "packaging/fedora/autozcoder.spec"])
        self.assertEqual(result.returncode, 1)

    def test_vendor_license_complete(self):
        text = (ROOT / "tools/captcha-mint/LICENSE.vendor").read_text()
        self.assertIn("TriDefender", text)
        self.assertIn("Attribution-NonCommercial-ShareAlike 4.0", text)
        self.assertIn("Section 8", text)
        self.assertGreater(len(text), 18000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
