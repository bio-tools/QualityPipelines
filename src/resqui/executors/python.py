import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

from resqui.executors.base import ExecutorInitError
from resqui.tools import normalized

CACHE_DIR_ENV = "RESQUI_CACHE_DIR"
_READY_MARKER = ".resqui-env-complete"


def cache_root():
    """Directory holding resqui's persistent caches.

    `RESQUI_CACHE_DIR` if set, else `$XDG_CACHE_HOME/resqui`, else
    `~/.cache/resqui`.
    """
    explicit = os.environ.get(CACHE_DIR_ENV)
    if explicit:
        return explicit
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "resqui")


def _is_pinned(spec):
    """True if `spec` always resolves to the same release.

    Only pinned specs may be cached, otherwise a cached environment would
    keep serving an old release forever. Accepted are exact `==` versions
    (without a wildcard) and git URLs with a `@<ref>` suffix. A git ref is
    assumed to be a tag or commit, not a moving branch.
    """
    if spec.startswith("git+"):
        if "@" not in spec:
            return False
        ref = spec.rsplit("@", 1)[1]
        return bool(ref) and "/" not in ref and ":" not in ref
    return "==" in spec and not spec.rstrip().endswith("*")


def _env_key(specs):
    """Cache key of an environment: the specs plus the interpreter they were
    built with (a venv is tied to the base Python it was created from)."""
    identity = {
        "specs": sorted(set(specs)),
        "python": sys.version,
        "base_prefix": sys.base_prefix,
        "machine": platform.machine(),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[
        :32
    ]


def _env_is_ready(env_dir):
    """A cached environment is usable if it finished building and its
    interpreter still resolves (the base Python may have been removed)."""
    return os.path.isfile(os.path.join(env_dir, _READY_MARKER)) and os.path.exists(
        os.path.join(env_dir, "bin", "python")
    )


@contextlib.contextmanager
def _exclusive_lock(path):
    """Cross-process lock, so concurrent runs build an environment only once."""
    with open(path, "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


class PythonExecutor:
    """A Python executor which uses a virtual environment.

    The `packages` should be a list of package names with optional
    requirement specifiers as accepted by `pip`.

    By default the environment is a temporary folder, removed when the
    executor is destroyed.

    With `cache=True` the environment is kept between runs under
    `cache_root()/envs` (set `RESQUI_CACHE_DIR` to move it). It is looked up
    by the exact set of installed packages and the running Python, and is
    built only if missing; concurrent runs wait for each other rather than
    building twice. Cached environments are never modified: calling
    `install()` switches the executor to the environment for the larger set
    of packages. Only pinned specs (`name==1.2.3`, `git+https://...@tag`) are
    cached; any other spec makes the executor use a temporary environment
    instead. To clear the cache, delete the `envs` folder.

    More information:
    https://packaging.python.org/en/latest/glossary/#term-Requirement-Specifier

    """

    def __init__(self, packages=None, environment=None, cache=False):
        """Instantiates a virtual environment (in a temporary folder or, with
        `cache=True`, in the persistent cache)."""
        self.environment = environment if environment is not None else {}
        self.packages = []
        self._cache = bool(cache) and fcntl is not None
        self._env_dir = None
        self._owns_env_dir = False
        try:
            if self._cache:
                # The environment depends on the packages, so it is chosen
                # once they are known (here, or on the first install()).
                if packages:
                    self.packages = list(packages)
                    self._select_env()
                return
            self._env_dir = tempfile.mkdtemp()
            self._owns_env_dir = True
            venv.create(self._env_dir, with_pip=True, symlinks=(os.name != "nt"))
            if packages is None:
                return
            for package in packages:
                self.install(package)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            raise ExecutorInitError(f"failed to initialise Python executor: {e}")

    @property
    def temp_dir(self):
        """Folder of the environment in use (its `bin/` holds installed tools)."""
        if self._env_dir is None:
            try:
                self._select_env()
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                raise ExecutorInitError(f"failed to initialise Python executor: {e}")
        return self._env_dir

    def _pip_install(self, env_dir, package):
        subprocess.run(
            [f"{env_dir}/bin/pip", "install", package],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _drop_owned_env(self):
        if self._owns_env_dir and self._env_dir is not None:
            shutil.rmtree(self._env_dir, ignore_errors=True)
        self._env_dir = None
        self._owns_env_dir = False

    def _select_env(self):
        """Point this executor at the environment for `self.packages`."""
        specs = list(dict.fromkeys(self.packages))
        self._drop_owned_env()

        if not all(_is_pinned(spec) for spec in specs):
            # Not safely cacheable: build a private, temporary environment.
            self._env_dir = tempfile.mkdtemp()
            self._owns_env_dir = True
            venv.create(self._env_dir, with_pip=True, symlinks=(os.name != "nt"))
            for spec in specs:
                self._pip_install(self._env_dir, spec)
            return

        root = os.path.join(cache_root(), "envs")
        os.makedirs(root, mode=0o700, exist_ok=True)
        key = _env_key(specs)
        env_dir = os.path.join(root, key)
        with _exclusive_lock(os.path.join(root, f"{key}.lock")):
            if not _env_is_ready(env_dir):
                # Missing, half-built or broken: start from scratch. A venv
                # is not relocatable, so it must be built where it will live.
                shutil.rmtree(env_dir, ignore_errors=True)
                try:
                    venv.create(env_dir, with_pip=True, symlinks=(os.name != "nt"))
                    for spec in specs:
                        self._pip_install(env_dir, spec)
                    with open(os.path.join(env_dir, _READY_MARKER), "w") as marker:
                        json.dump({"specs": specs, "python": sys.version}, marker)
                except BaseException:
                    shutil.rmtree(env_dir, ignore_errors=True)
                    raise
        self._env_dir = env_dir

    def install(self, package):
        try:
            if self._cache:
                self.packages.append(package)
                try:
                    self._select_env()
                except BaseException:
                    self.packages.remove(package)
                    raise
                return
            self._pip_install(self.temp_dir, package)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            raise ExecutorInitError(f"failed to initialise Python executor: {e}")

    def is_installed(self, package_name, version=None):
        out = self.execute(
            normalized(
                """
            from importlib.metadata import distributions
            for dist in distributions():
                name = dist.metadata.get('Name', '<unknown>')
                version = getattr(dist, 'version', '<unknown>')
                print(f"{name}=={version}")
        """
            )
        )
        package = package_name + "" if version is None else f"=={version}"
        return package in out.stdout

    def execute(self, script):
        """Run the script in the virtual environment."""
        env = os.environ.copy()
        env.update(self.environment)
        return subprocess.run(
            [f"{self.temp_dir}/bin/python", "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )

    def __del__(self):
        """Cleanup a temporary virtual environment on destruction.

        Cached environments are left in place."""
        try:
            env_dir = getattr(self, "_env_dir", None)
            if getattr(self, "_owns_env_dir", False) and env_dir:
                if os.path.exists(env_dir):
                    shutil.rmtree(env_dir)
        except Exception as e:
            print(f"Failed to remove virtualenv at {getattr(self, '_env_dir', None)}: {e}")
