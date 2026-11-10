import os
import shutil
import subprocess
from typing import Optional
import tempfile
from dataclasses import dataclass

SHARED_WORKDIR_ENV = "RESQUI_SHARED_WORKDIR"
DOCKER_WORK_VOLUME_ENV = "RESQUI_DOCKER_WORK_VOLUME"

CLONE_TIMEOUT_SECONDS = 600

# One source clone per repository URL, reused by all plugins of a run. It is
# never used as a working tree; plugins get their own copy via a local
# `git clone`, which reuses its objects through hardlinks.
_workspace_sources: dict = {}


@dataclass(frozen=True)
class Workspace:
    """Workspace visible to resqui and, optionally, Docker plugin containers."""

    local_path: str

    shared_root: Optional[str] = None
    docker_volume: Optional[str] = None

    @property
    def is_shared(self) -> bool:
        return self.shared_root is not None and self.docker_volume is not None

    def container_path(self, fallback_path: str) -> str:
        """Return the path the plugin container should use for this workspace."""
        if self.is_shared:
            return self.local_path
        return fallback_path

    def docker_mount_args(self, fallback_path: str) -> list[str]:
        """Return Docker -v arguments for exposing this workspace to a plugin."""
        if self.is_shared:
            return ["-v", f"{self.docker_volume}:{self.shared_root}"]
        return ["-v", f"{self.local_path}:{fallback_path}"]

    def checkout(self, ref: str) -> None:
        """Check out a branch, tag or commit inside this workspace's copy."""
        subprocess.run(
            ["git", "checkout", ref],
            check=True,
            cwd=self.local_path,
            timeout=CLONE_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.local_path, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cleanup()


def clear_workspace_cache() -> None:
    """Remove all cached source clones (e.g. after a CLI run)."""
    for source_path in list(_workspace_sources.values()):
        shutil.rmtree(source_path, ignore_errors=True)
    _workspace_sources.clear()


def _is_shallow(path: str) -> bool:
    """True if `path` is a shallow clone (bare or with a working tree)."""
    return os.path.exists(os.path.join(path, "shallow")) or os.path.exists(
        os.path.join(path, ".git", "shallow")
    )


def _seed_source(url: str, timeout: Optional[int]) -> str:
    """Clone `url` once into a bare directory used as shared source.

    A bare clone keeps every branch under refs/heads/, so plugin copies
    cloned from it expose all branches as refs/remotes/origin/*.
    """
    source_path = tempfile.mkdtemp(prefix="resqui-source-")
    try:
        subprocess.run(
            ["git", "clone", "--bare", "--", url, source_path],
            check=True,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        shutil.rmtree(source_path, ignore_errors=True)
        print(f"Error cloning {url}: {e}")
        raise
    return source_path


def _get_source(url: str, timeout: Optional[int]) -> Optional[str]:
    """Return the cached source clone path for `url`, cloning it if absent.

    Falls back to cloning from the network when the cached source turns out
    to be shallow, since ref checkouts in plugin copies would fail.
    """
    source_path = _workspace_sources.get(url)
    if source_path is not None:
        if not _is_shallow(source_path):
            return source_path
        shutil.rmtree(source_path, ignore_errors=True)
        _workspace_sources.pop(url, None)

    source_path = _seed_source(url, timeout)
    if _is_shallow(source_path):
        # A shallow source cannot serve arbitrary ref checkouts downstream;
        # keep it only for a plain (no-ref) workspace copy.
        return source_path
    _workspace_sources[url] = source_path
    return source_path


def _populate_from_source(
    source_path: str, local_path: str, timeout: Optional[int]
) -> None:
    """Fill a workspace by cloning the bare cached source (local, hardlinked)."""
    subprocess.run(
        ["git", "clone", "--", source_path, local_path],
        check=True,
        timeout=timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def create_workspace(
    prefix: str = "resqui-",
    repo_source: Optional[str] = None,
    clone_timeout: Optional[int] = CLONE_TIMEOUT_SECONDS,
) -> Workspace:
    """Create a temporary workspace for local and Docker-backed plugin runs.

    By default, this behaves like tempfile.mkdtemp(). In worker mode, set
    RESQUI_SHARED_WORKDIR and RESQUI_DOCKER_WORK_VOLUME so plugin containers can
    mount the same Docker volume that the worker uses.

    With repo_source set, the workspace is seeded with a copy of that
    repository's run-scoped source clone instead of being left empty. The
    source is cloned from the network once per run and URL; subsequent
    workspaces for the same repository are cloned locally from it.
    """
    shared_root = os.getenv(SHARED_WORKDIR_ENV)
    docker_volume = os.getenv(DOCKER_WORK_VOLUME_ENV)

    if bool(shared_root) != bool(docker_volume):
        raise ValueError(
            f"{SHARED_WORKDIR_ENV} and {DOCKER_WORK_VOLUME_ENV} must be set together"
        )

    if shared_root and docker_volume:
        os.makedirs(shared_root, exist_ok=True)
        local_path = tempfile.mkdtemp(prefix=prefix, dir=shared_root)
        workspace = Workspace(
            local_path=os.path.abspath(local_path),
            shared_root=os.path.abspath(shared_root),
            docker_volume=docker_volume,
        )
    else:
        workspace = Workspace(local_path=tempfile.mkdtemp(prefix=prefix))

    if repo_source is None:
        return workspace

    try:
        source_path = _get_source(repo_source, clone_timeout)
        _populate_from_source(source_path, workspace.local_path, clone_timeout)
    except subprocess.CalledProcessError:
        workspace.cleanup()
        raise
    return workspace
