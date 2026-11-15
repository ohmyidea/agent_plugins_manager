"""Git checkout and revision helpers."""

import os
import subprocess
from pathlib import Path
from urllib import request

from .tree import SyncError, path_parts


def _git_environment() -> dict[str, str]:
    """Let Git inherit Windows' manual proxy when the shell has no proxy set."""
    env = os.environ.copy()
    configured = {name.lower() for name in env}
    # An explicitly empty variable also counts as a user's proxy choice.
    if os.name == "nt" and not configured.intersection({"http_proxy", "https_proxy", "all_proxy"}):
        for protocol, proxy in request.getproxies_registry().items():
            name = f"{protocol}_proxy"
            if protocol in {"http", "https", "all", "no"} and name not in configured:
                env[name.upper()] = proxy
    return env


def git(*args: str, cwd: Path, input: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=cwd, input=input, capture_output=True, encoding="utf-8", errors="replace",
        env=_git_environment(),
    )
    if result.returncode:
        raise SyncError(result.stderr.strip() or f"git {args[0]} failed ({result.returncode})")
    return result.stdout.strip()


def checkout(
    repo: str, destination: Path, *, branch: str = "", tag: str = "", commit: str = "",
) -> str:
    """Fetch one shallow commit, tag or branch, then check out portable filenames."""
    if commit:
        destination.mkdir()
        object_format = ("--object-format=sha256",) if len(commit) == 64 else ()
        git("init", "--quiet", *object_format, cwd=destination)
        git("fetch", "--depth=1", "--no-tags", "--", repo, commit.lower(), cwd=destination)
        revision = git("rev-parse", "--verify", "FETCH_HEAD^{commit}", cwd=destination)
        if revision != commit.lower():
            raise SyncError(f"Fetched commit {revision} does not match requested commit {commit}")
        git("update-ref", "--no-deref", "HEAD", revision, cwd=destination)
    else:
        name = tag.removeprefix("refs/tags/") or branch.removeprefix("refs/heads/")
        selection = ("--branch", name) if name else ()
        git(
            "clone", "--depth=1", "--single-branch", "--no-tags", "--no-checkout",
            *selection, "--", repo, str(destination), cwd=destination.parent,
        )
        revision = git("rev-parse", "--verify", "HEAD^{commit}", cwd=destination)
    entries = git("ls-tree", "-rz", "--name-only", revision, cwd=destination)
    # Zone.Identifier streams are Windows download metadata accidentally committed
    # upstream. Filter them before populating the index, keeping NTFS protection on.
    paths = []
    for path in entries.split("\0"):
        if not path:
            continue
        if any(part.lower().endswith(":zone.identifier") for part in path.split("/")):
            continue
        if os.name == "nt":
            path_parts(path)
        paths.append(path + "\0")
    if paths:
        git(
            "-c", "core.longpaths=true", "--literal-pathspecs", "restore", "--source=HEAD", "--staged", "--worktree",
            "--pathspec-from-file=-", "--pathspec-file-nul", cwd=destination, input="".join(paths),
        )
    return revision
