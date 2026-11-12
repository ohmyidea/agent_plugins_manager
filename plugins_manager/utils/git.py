"""Git checkout and revision helpers."""

import subprocess
from pathlib import Path

from .tree import SyncError


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise SyncError(result.stderr.strip() or f"git {args[0]} failed ({result.returncode})")
    return result.stdout.strip()


def checkout(repo: str, ref: str, destination: Path) -> str:
    destination.mkdir()
    git("init", "--quiet", cwd=destination)
    git("fetch", "--depth=1", "--no-tags", "--", repo, ref, cwd=destination)
    commit = git("rev-parse", "--verify", "FETCH_HEAD^{commit}", cwd=destination)
    git("checkout", "--detach", commit, cwd=destination)
    return commit


def source_version(repo: str, commit: str, cwd: Path) -> str:
    refs = git("ls-remote", "--tags", "--sort=-version:refname", "--", repo, cwd=cwd)
    for line in refs.splitlines():
        target, ref = line.split("\t", 1)
        if target == commit:
            tag = ref.removeprefix("refs/tags/").removesuffix("^{}")
            return tag[1:] if len(tag) > 1 and tag[0] in "vV" and tag[1].isdigit() else tag
    return commit[:7]
