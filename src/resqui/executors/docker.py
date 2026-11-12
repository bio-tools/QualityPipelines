import subprocess
from resqui.executors.base import ExecutorInitError


def _is_mutable_reference(image_url):
    """True if the reference can point to different content over time.

    Digest references are immutable. Tagged references are treated as
    immutable too, except `latest` (and untagged names, which mean `latest`).
    """
    if "@" in image_url:
        return False
    last_segment = image_url.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return True
    return last_segment.rsplit(":", 1)[1] == "latest"


def _requested_platform(pull_args):
    """Return the value of `--platform` in pull_args, or None."""
    for i, arg in enumerate(pull_args):
        if arg == "--platform" and i + 1 < len(pull_args):
            return pull_args[i + 1]
        if arg.startswith("--platform="):
            return arg.split("=", 1)[1]
    return None


def image_is_local(image_url, platform=None):
    """True if the image (optionally for `platform`, e.g. linux/amd64) is
    already in the local Docker image store."""
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}}",
                image_url,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    if platform is None:
        return True
    # Compare os/arch only; ignore a variant suffix such as linux/arm64/v8.
    return result.stdout.strip() == "/".join(platform.split("/")[:2])


def ensure_image(image_url, pull_args=None):
    """Make sure `image_url` is available locally.

    The registry is only contacted when the image is missing locally or when
    its reference is mutable (`latest`). Pinned tags and digests are reused
    as they are, which saves a registry round-trip per plugin on every run.

    Raises subprocess.CalledProcessError when a needed pull fails.
    """
    if pull_args is None:
        pull_args = []
    if not _is_mutable_reference(image_url) and image_is_local(
        image_url, _requested_platform(pull_args)
    ):
        return
    subprocess.run(
        ["docker", "pull"] + pull_args + [image_url],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class DockerExecutor:
    """A Docker executor."""

    def __init__(self, image_url, pull_args=None):
        self.url = image_url
        if pull_args is None:
            pull_args = []
        command = ["docker", "pull"] + pull_args + [self.url]
        try:
            ensure_image(self.url, pull_args)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise ExecutorInitError(
                f"failed to initialise Docker executor: '{' '.join(command)}'"
            )

    def run(self, command, run_args=None):
        """
        Run command (popenargs) inside a Docker container and return a
        CompletedProcess instance from the subprocess Python module.

        Extra arguments to the command can be passed as a list of
        strings via `run_args`.
        """
        if run_args is None:
            run_args = []
        cmd = ["docker", "run"] + run_args + [self.url] + command
        return subprocess.run(cmd, capture_output=True, text=True)
