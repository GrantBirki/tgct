#!/usr/bin/env python3
"""Build candidate content with the reviewed Hugo package from trusted tooling."""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

# https://github.com/gohugoio/hugo/releases/download/v0.119.0/hugo_0.119.0_checksums.txt
VERSION = "0.119.0"
PACKAGE = f"hugo_extended_{VERSION}_linux-amd64.deb"
DIGEST = "b88f825992b89915fe1ec46ddc16578d28ca37d17759922620f67adbac04d0d6"


def regular_file(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Tooling and output paths must not contain symlinks")
    if not path.is_file():
        raise ValueError("Required regular file is missing")
    return path


def verify_checkout(path, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("An exact commit SHA is required")
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Checkout must be a real directory")
    actual = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if actual != sha:
        raise ValueError("Checkout does not match its selected commit")


def verified_package(trusted):
    if regular_file(trusted, ".hugo-version").read_text().strip() != VERSION:
        raise ValueError("Hugo version does not match the reviewed toolchain")
    package = regular_file(trusted, "vendor/" + PACKAGE)
    if hashlib.sha256(package.read_bytes()).hexdigest() != DIGEST:
        raise ValueError("Hugo package checksum does not match")
    return package


def build(trusted, candidate, trusted_sha, selected_sha, temp, base_url=""):
    verify_checkout(trusted, trusted_sha)
    verify_checkout(candidate, selected_sha)
    trusted, candidate, temp = trusted.resolve(), candidate.resolve(), temp.resolve()
    if trusted == candidate or trusted in candidate.parents or candidate in trusted.parents:
        raise ValueError("Checkouts must be separate directories")
    if any(root == temp or root in temp.parents for root in (trusted, candidate)):
        raise ValueError("Build storage must be outside the checkouts")
    package = verified_package(trusted)
    work = Path(tempfile.mkdtemp(prefix="hugo-build-", dir=temp))
    tools = work / "tools"
    subprocess.run(["dpkg-deb", "--extract", str(package), str(tools)], check=True)
    hugo = regular_file(tools, "usr/local/bin/hugo")
    version = subprocess.run([str(hugo), "version"], check=True, capture_output=True, text=True).stdout
    if not version.startswith("hugo v" + VERSION + "-") or "+extended" not in version or "linux/amd64" not in version:
        raise ValueError("Unexpected Hugo executable version or edition")
    output = work / "public"
    command = [str(hugo), "--gc", "--logLevel", "info", "--source", str(candidate),
               "--destination", str(output), "--cacheDir", str(work / "cache")]
    if base_url:
        command += ["--baseURL", base_url]
    subprocess.run(command, cwd=candidate, check=True,
                   env={**os.environ, "HUGO_RESOURCEDIR": str(work / "resources")})
    if output.is_symlink() or not output.is_dir():
        raise ValueError("Build output must be a real directory")
    version_file = output / "version.txt"
    if version_file.is_symlink() or (version_file.exists() and not version_file.is_file()):
        raise ValueError("Version output must be a regular file")
    version_file.write_text(selected_sha + "\n")
    return output


if __name__ == "__main__":
    output = build(Path("trusted"), Path("candidate"), os.environ["TRUSTED_SHA"],
                   os.environ["SELECTED_SHA"], Path(os.environ["RUNNER_TEMP"]),
                   os.environ.get("BASE_URL", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        handle.write(f"path={output}\n")
