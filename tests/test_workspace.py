import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from resqui.workspace import (
    _workspace_sources,
    clear_workspace_cache,
    create_workspace,
    register_workspace_source,
)


class TestWorkspace(unittest.TestCase):
    def test_local_workspace_mounts_temp_dir_to_requested_container_path(self):
        with patch.dict(os.environ, {}, clear=True):
            workspace = create_workspace(prefix="test-resqui-")

        try:
            self.assertTrue(os.path.isdir(workspace.local_path))
            self.assertEqual(workspace.container_path("/path"), "/path")
            self.assertEqual(
                workspace.docker_mount_args("/path"),
                ["-v", f"{workspace.local_path}:/path"],
            )
        finally:
            workspace.cleanup()

        self.assertFalse(os.path.exists(workspace.local_path))

    def test_shared_workspace_mounts_named_volume_and_keeps_same_path(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(
                os.environ,
                {
                    "RESQUI_SHARED_WORKDIR": root,
                    "RESQUI_DOCKER_WORK_VOLUME": "sqoo_resqui_work",
                },
                clear=True,
            ):
                workspace = create_workspace(prefix="test-resqui-")

            try:
                self.assertTrue(workspace.local_path.startswith(root))
                self.assertTrue(os.path.isdir(workspace.local_path))
                self.assertEqual(
                    workspace.container_path("/path"), workspace.local_path
                )
                self.assertEqual(
                    workspace.docker_mount_args("/path"),
                    ["-v", f"sqoo_resqui_work:{root}"],
                )
            finally:
                workspace.cleanup()

            self.assertFalse(os.path.exists(workspace.local_path))

    def test_partial_shared_workspace_configuration_is_rejected(self):
        with patch.dict(
            os.environ,
            {"RESQUI_SHARED_WORKDIR": "/resqui-work"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                create_workspace()


class TestWorkspaceSourceCache(unittest.TestCase):
    def setUp(self):
        clear_workspace_cache()
        self.addCleanup(clear_workspace_cache)

    def _fake_git_factory(self):
        """Track git invocations; make clone materialize a minimal repo."""
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            fake_run.kwargs_cwd = kwargs.get("cwd")
            if args[:2] == ["git", "clone"] and args[1] != "checkout":
                dest = args[-1]
                git_dir = os.path.join(dest, ".git")
                os.makedirs(git_dir, exist_ok=True)
            return MagicMock(returncode=0)

        fake_run.kwargs_cwd = None
        return calls, fake_run

    def test_repo_source_clones_network_once_then_locally(self):
        calls, fake_run = self._fake_git_factory()
        url = "https://github.com/example/repo"
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=fake_run):
                with create_workspace(prefix="t1-", repo_source=url) as first:
                    self.assertTrue(
                        os.path.isdir(os.path.join(first.local_path, ".git"))
                    )
                with create_workspace(prefix="t2-", repo_source=url) as second:
                    self.assertTrue(
                        os.path.isdir(os.path.join(second.local_path, ".git"))
                    )

        clones = [c for c in calls if c[:2] == ["git", "clone"]]
        # 1 network clone + 1 local clone per workspace. Bare seeds put the
        # URL at [-2] (dest is [-1]); plugin copies put the source at [-1].
        self.assertEqual(len(clones), 3)
        self.assertEqual(clones[0][-2], url)
        self.assertEqual(clones[1][-2], _workspace_sources[url])
        self.assertEqual(clones[2][-2], _workspace_sources[url])
        self.assertNotEqual(first.local_path, second.local_path)

    def test_cached_source_not_removed_by_workspace_cleanup(self):
        calls, fake_run = self._fake_git_factory()
        url = "https://github.com/example/repo"
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=fake_run):
                with create_workspace(prefix="t-", repo_source=url):
                    pass
                source_path = _workspace_sources[url]
                self.assertTrue(os.path.isdir(source_path))
        self.assertTrue(os.path.isdir(source_path))

    def test_clear_workspace_cache_removes_sources(self):
        calls, fake_run = self._fake_git_factory()
        url = "https://github.com/example/repo"
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=fake_run):
                with create_workspace(prefix="t-", repo_source=url):
                    pass
                source_path = _workspace_sources[url]
                clear_workspace_cache()
        self.assertFalse(os.path.exists(source_path))
        self.assertEqual(_workspace_sources, {})

    def test_checkout_runs_git_in_workspace(self):
        calls, fake_run = self._fake_git_factory()
        url = "https://github.com/example/repo"
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=fake_run):
                with create_workspace(prefix="t-", repo_source=url) as ws:
                    ws.checkout("v1.2.0")

        checkouts = [c for c in calls if c[:2] == ["git", "checkout"]]
        self.assertEqual(checkouts, [["git", "checkout", "v1.2.0"]])
        # checkout() used cwd, not a -C flag: run() got the workspace as cwd.
        self.assertEqual(fake_run.kwargs_cwd, ws.local_path)

    def test_clone_failure_cleans_up_workspace(self):
        def failing_run(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                raise subprocess.CalledProcessError(128, "git")

        url = "https://github.com/example/repo"
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=failing_run):
                with self.assertRaises(subprocess.CalledProcessError):
                    create_workspace(prefix="t-", repo_source=url)
        self.assertEqual(
            [d for d in os.listdir(tempfile.gettempdir()) if d.startswith("t-")],
            [],
        )
        self.assertNotIn(url, _workspace_sources)

    def test_shallow_cached_source_is_replaced_on_next_request(self):
        calls, fake_run = self._fake_git_factory()

        def shallow_run(args, **kwargs):
            fake_run(args, **kwargs)
            if args[:2] == ["git", "clone"] and args[1] != "checkout":
                open(os.path.join(args[-1], ".git", "shallow"), "w").close()

        url = "https://github.com/example/repo"
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=shallow_run):
                with create_workspace(prefix="t1-", repo_source=url) as first:
                    pass
                # Shallow seed was usable for this copy but never cached.
                self.assertNotIn(url, _workspace_sources)
                with create_workspace(prefix="t2-", repo_source=url) as second:
                    pass

        clones = [c for c in calls if c[:2] == ["git", "clone"]]
        # 2 network seeds + 2 local copies. Bare seeds put the URL at [-2];
        # local copies put the source path at [-2]. The shallow seed still
        # serves the request that created it, but is never cached, so the
        # next workspace request reseeds from the network.
        self.assertEqual(len(clones), 4)
        self.assertEqual(clones[0][-2], url)
        self.assertNotEqual(clones[1][-2], url)
        self.assertEqual(clones[2][-2], url)
        self.assertNotEqual(clones[3][-2], url)

    def test_workspace_copy_is_checked_out_before_plugin_use(self):
        # Document ordering: the copy is complete when create_workspace returns.
        calls, fake_run = self._fake_git_factory()
        url = "https://github.com/example/repo"
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=fake_run):
                with create_workspace(prefix="t-", repo_source=url) as ws:
                    self.assertTrue(os.path.isdir(ws.local_path))
                    self.assertTrue(os.path.isdir(os.path.join(ws.local_path, ".git")))
        self.assertEqual(calls[-1][:2], ["git", "clone"])

    def test_register_workspace_source_seeds_cache_without_cloning(self):
        url = "https://github.com/example/repo"
        with tempfile.TemporaryDirectory() as source:
            register_workspace_source(url, source)
            self.assertEqual(_workspace_sources[url], source)

            calls, fake_run = self._fake_git_factory()
            with patch.dict(os.environ, {}, clear=True):
                with patch("resqui.workspace.subprocess.run", side_effect=fake_run):
                    with create_workspace(prefix="t-", repo_source=url) as ws:
                        pass

        clones = [c for c in calls if c[:2] == ["git", "clone"]]
        # Only the local workspace copy; no network clone was needed.
        self.assertEqual(len(clones), 1)
        self.assertEqual(clones[0][-1], ws.local_path)
        self.assertIn(source, clones[0])

    def test_register_workspace_source_replaces_previous_entry(self):
        url = "https://github.com/example/repo"
        calls, fake_run = self._fake_git_factory()
        with patch.dict(os.environ, {}, clear=True):
            with patch("resqui.workspace.subprocess.run", side_effect=fake_run):
                with create_workspace(prefix="t-", repo_source=url):
                    pass
                old_source = _workspace_sources[url]
                with tempfile.TemporaryDirectory() as new_source:
                    register_workspace_source(url, new_source)
                    self.assertEqual(_workspace_sources[url], new_source)
                self.assertFalse(os.path.exists(old_source))

    def test_clear_workspace_cache_removes_registered_source(self):
        url = "https://github.com/example/repo"
        with tempfile.TemporaryDirectory() as source:
            register_workspace_source(url, source)
            clear_workspace_cache()
            self.assertFalse(os.path.exists(source))
            self.assertEqual(_workspace_sources, {})
