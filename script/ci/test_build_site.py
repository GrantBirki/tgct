#!/usr/bin/env python3
"""Secretless checks of the trusted Hugo build boundary."""

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("build_site", Path(__file__).with_name("build_site.py"))
build_site = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_site)
SHA = "a" * 40
SELECTED = "b" * 40


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.trusted = self.root / "trusted"
        self.candidate = self.root / "candidate"
        self.trusted.mkdir()
        self.candidate.mkdir()
        (self.trusted / "vendor").mkdir()
        (self.trusted / ".hugo-version").write_text(build_site.VERSION + "\n")
        self.package = self.trusted / "vendor" / build_site.PACKAGE
        self.package.write_bytes(b"reviewed test package")
        self.addCleanup(patch.stopall)
        patch.object(build_site, "DIGEST", hashlib.sha256(self.package.read_bytes()).hexdigest()).start()
        self.calls = []
        patch.object(build_site.subprocess, "run", side_effect=self.run_command).start()

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == "git":
            return SimpleNamespace(stdout=(SHA if command[2] == str(self.trusted) else SELECTED) + "\n")
        if command[0] == "dpkg-deb":
            binary = Path(command[-1]) / "usr/local/bin/hugo"
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture")
        elif command[-1] == "version":
            return SimpleNamespace(stdout="hugo v0.119.0-test+extended linux/amd64")
        else:
            output = Path(command[command.index("--destination") + 1])
            output.mkdir()
            (output / "index.html").write_text("rendered candidate content")
        return SimpleNamespace(stdout="")

    def build(self, **overrides):
        args = dict(trusted=self.trusted, candidate=self.candidate, trusted_sha=SHA,
                    selected_sha=SELECTED, temp=self.root, base_url="https://example.com/")
        args.update(overrides)
        return build_site.build(**args)

    def test_candidate_tooling_cannot_select_the_binary(self):
        (self.candidate / ".hugo-version").write_text("999.0.0")
        (self.candidate / "vendor").mkdir()
        (self.candidate / "vendor" / build_site.PACKAGE).write_text("candidate package")
        (self.candidate / "script/ci").mkdir(parents=True)
        candidate_helper = self.candidate / "script/ci/build_site.py"
        candidate_helper.write_text("raise RuntimeError('candidate helper')")
        (self.candidate / "vendor/checksums.txt").write_text("candidate digest")
        output = self.build()
        self.assertEqual((output / "version.txt").read_text(), SELECTED + "\n")
        extraction = next(call for call in self.calls if call[0] == "dpkg-deb")
        self.assertEqual(extraction[2], str(self.package))
        render = self.calls[-1]
        self.assertTrue(Path(render[0]).is_absolute())
        self.assertEqual(render[render.index("--source") + 1], str(self.candidate))
        self.assertEqual(render[-2:], ["--baseURL", "https://example.com/"])
        self.assertNotIn(self.trusted, output.parents)
        self.assertNotIn(self.candidate, output.parents)
        self.assertFalse(any(str(candidate_helper) in call for call in self.calls))
        self.assertNotIn("sudo", str(self.calls))

    def test_invalid_or_mismatched_sha_stops_before_extraction(self):
        for sha in ("", "main", "../candidate", "a" * 39, "c" * 40):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                self.build(selected_sha=sha)
        self.assertFalse(any(call[0] == "dpkg-deb" for call in self.calls))

    def test_tampered_trusted_package_stops_before_extraction(self):
        self.package.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(len(self.calls), 2)

    def test_trusted_version_is_checked(self):
        (self.trusted / ".hugo-version").write_text("../../unexpected")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(len(self.calls), 2)

    def test_symlinked_tooling_is_rejected(self):
        self.package.unlink()
        self.package.symlink_to(self.root / "elsewhere")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(len(self.calls), 2)

    def test_missing_package_is_rejected(self):
        self.package.unlink()
        with self.assertRaises(ValueError):
            self.build()

    def test_colliding_checkouts_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(candidate=self.trusted, selected_sha=SHA)
        self.assertEqual(len(self.calls), 2)

    def test_output_storage_cannot_overlap_checkouts(self):
        with self.assertRaises(ValueError):
            self.build(temp=self.trusted)
        self.assertEqual(len(self.calls), 2)

    def test_symlinked_checkout_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.candidate, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.build(candidate=alias)

    def test_render_failure_does_not_return_an_artifact(self):
        original = self.run_command
        def fail_render(command, **kwargs):
            if "--destination" in command:
                raise subprocess.CalledProcessError(1, command)
            return original(command, **kwargs)
        with patch.object(build_site.subprocess, "run", side_effect=fail_render):
            with self.assertRaises(subprocess.CalledProcessError):
                self.build()

    def test_unexpected_binary_version_is_rejected(self):
        original = self.run_command
        def wrong_version(command, **kwargs):
            if command[-1] == "version":
                return SimpleNamespace(stdout="hugo v999.0.0")
            return original(command, **kwargs)
        with patch.object(build_site.subprocess, "run", side_effect=wrong_version):
            with self.assertRaises(ValueError):
                self.build()
        self.assertFalse(any("--destination" in call for call in self.calls))

    def test_nested_output_links_and_special_files_are_rejected(self):
        for kind in ("symlink", "directory-link", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                original = self.run_command
                def unsafe_output(command, **kwargs):
                    result = original(command, **kwargs)
                    if "--destination" in command:
                        output = Path(command[command.index("--destination") + 1])
                        nested = output / "assets"
                        nested.mkdir()
                        target = nested / "unexpected"
                        if kind == "symlink":
                            target.symlink_to(self.package)
                        elif kind == "directory-link":
                            target.symlink_to(self.trusted, target_is_directory=True)
                        elif kind == "hardlink":
                            os.link(self.package, target)
                        else:
                            os.mkfifo(target)
                    return result
                with patch.object(build_site.subprocess, "run", side_effect=unsafe_output):
                    with self.assertRaises(ValueError):
                        self.build()
                self.assertEqual(self.package.read_bytes(), b"reviewed test package")

    def test_missing_index_is_rejected(self):
        original = self.run_command
        def missing_index(command, **kwargs):
            result = original(command, **kwargs)
            if "--destination" in command:
                output = Path(command[command.index("--destination") + 1])
                (output / "index.html").unlink()
            return result
        with patch.object(build_site.subprocess, "run", side_effect=missing_index):
            with self.assertRaises(ValueError):
                self.build()

    def test_symlinked_version_output_is_rejected(self):
        original = self.run_command
        def unsafe_output(command, **kwargs):
            result = original(command, **kwargs)
            if "--destination" in command:
                output = Path(command[command.index("--destination") + 1])
                (output / "version.txt").symlink_to(self.package)
            return result
        with patch.object(build_site.subprocess, "run", side_effect=unsafe_output):
            with self.assertRaises(ValueError):
                self.build()
        self.assertEqual(self.package.read_bytes(), b"reviewed test package")


if __name__ == "__main__":
    unittest.main()
