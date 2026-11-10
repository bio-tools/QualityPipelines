import atexit
import socket
import subprocess
import time
import weakref

import requests

from resqui.plugins.base import IndicatorPlugin
from resqui.executors import DockerExecutor
from resqui.core import CheckResult

TEST_IRI_SUFFIX = "/test/"


class FAIRChecker(IndicatorPlugin):
    """Evaluates FAIRness with the [FAIR-Checker](https://github.com/IFB-ElixirFr/fair-checker)
    tool, developed at IFB/Elixir France.

    Languages: language-independent.

    FAIR-Checker has no way to assess a specific commit: its
    /api/check/metrics_all endpoint evaluates the live repository page.
    branch_hash_or_tag is therefore only used for cache identity within a
    run and does not scope the results, and the evidence strings state the
    requested ref explicitly so reports stay auditable.
    """

    name = "FAIRChecker"
    version = "0.1"
    id = "https://w3id.org/everse/tools/fair-checker"
    image_url = "veitveit/fair-checker@sha256:c0dc5c3a332231992e1b62597572217c698e646863838827a5a15876d51c82a1"
    indicators = [
        "unique_ids",
        "persistent_ids",
        "structured_metadata",
        "shared_vocabularies_for_metadata",
        "machine_readable_format",
        "use_shared_ontologies",
        "external_links",
        "license_metadata",
    ]
    requires = ["docker"]

    def __init__(self, context):
        self.context = context
        self.executor = DockerExecutor(self.image_url)
        self._cache = {}
        self._container = None
        self._base_url = None
        atexit.register(self._cleanup, weakref.ref(self))

    def _free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _start_container(self):
        if self._container is not None:
            return
        port = self._free_port()
        self._base_url = f"http://127.0.0.1:{port}"
        name = f"resqui-fair-checker-{port}"
        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"{port}:5000",
            "-e",
            "FAIR_CHECKER_ENV=development",
            "-e",
            f"SERVER_URL={self._base_url}",
            "-v",
            "fair-checker-data:/data/db",
            self.image_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to start FAIR-Checker container: {result.stderr.strip()}"
            )
        self._container = name
        try:
            self._wait_for_api()
        except TimeoutError:
            self._cleanup()
            raise

    def _wait_for_api(self, timeout=300, poll_interval=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                # A healthy FAIR-Checker answers 500 on "/" (web UI error page)
                # while the API works; any HTTP response means the server is up.
                requests.get(self._base_url, timeout=5)
                return
            except requests.RequestException:
                pass
            time.sleep(poll_interval)
        raise TimeoutError(f"FAIR-Checker API did not become ready within {timeout}s")

    def _cleanup(self, plugin_ref=None):
        plugin = plugin_ref() if plugin_ref is not None else self
        if plugin is None:
            return
        if plugin._container is not None:
            subprocess.run(
                ["docker", "stop", plugin._container],
                capture_output=True,
                text=True,
            )
            plugin._container = None

    def execute(self, url, commit_hash):
        cache_key = (url, commit_hash)
        if cache_key in self._cache:
            return self._cache[cache_key]

        self._start_container()

        # FAIR-Checker cannot assess a specific ref; the requested
        # branch/hash is kept only for the evidence strings and the cache
        # identity, while the tool itself evaluates the live repository page.
        url = url.removesuffix(".git")

        response = requests.get(
            f"{self._base_url}/api/check/metrics_all",
            params={"url": url},
            timeout=300,
        )
        response.raise_for_status()
        report = response.json()

        results = self._parse_report(report, commit_hash)
        self._cache[cache_key] = results
        return results

    def _parse_report(self, report, branch_hash_or_tag=None):
        tests = {}
        for node in report:
            if "https://w3id.org/ftr#Test" in node.get("@type", []):
                tests[node["@id"]] = node

        results = {}
        for node in report:
            if "https://w3id.org/ftr#TestResult" not in node.get("@type", []):
                continue
            test_ids = [
                entry["@id"]
                for entry in node.get("https://w3id.org/ftr#outputFromTest", [])
            ]
            if not test_ids:
                continue
            test_id = test_ids[0]
            test_abbreviation = test_id.rsplit(TEST_IRI_SUFFIX, 1)[-1]
            test = tests.get(test_id, {})

            def _values(key):
                return [entry.get("@value") for entry in node.get(key, [])]

            def _test_values(key):
                return [entry.get("@value") for entry in test.get(key, [])]

            value = (_values("http://www.w3.org/ns/prov#value") or [None])[0]
            title = (_test_values("http://purl.org/dc/terms/title") or [None])[0]
            description = (
                _test_values("http://purl.org/dc/terms/description") or [None]
            )[0]
            target = [
                entry["@id"]
                for entry in node.get("https://w3id.org/ftr#assessmentTarget", [])
            ]
            timestamp = (
                _values("http://www.w3.org/ns/prov#generatedAtTime") or [None]
            )[0]

            if value is None:
                success = False
                output = "indeterminate"
            elif value == 0:
                success = False
                output = "false"
            elif value == 1:
                success = True
                output = "indeterminate"
            else:
                success = True
                output = "true"

            ref_note = (
                f" Requested ref {branch_hash_or_tag}; FAIR-Checker assesses the "
                "live repository page, not the ref."
                if branch_hash_or_tag
                else ""
            )
            evidence = (
                f"FAIR-Checker test {test_abbreviation}"
                + (f" ({title})" if title else "")
                + f" returned score {value}"
                + (f" for target {target[0]}" if target else "")
                + "."
                + (f" Test description: {description}" if description else "")
                + (f" Evaluated at {timestamp}." if timestamp else "")
                + ref_note
            )

            results[test_abbreviation] = CheckResult(
                process=(
                    "FAIR-Checker evaluates the repository against FAIR principles "
                    f"with test {test_abbreviation}"
                    + (f" '{title}'" if title else "")
                    + "."
                ),
                status_id="schema:CompletedActionStatus",
                output=output,
                evidence=evidence,
                success=success,
                raw_value=str(value),
            )
        return results

    def _indicator_checks(self, url, branch_hash_or_tag, abbreviation):
        report = self.execute(url, branch_hash_or_tag)
        if abbreviation not in report:
            raise ValueError(
                f"FAIR-Checker did not return a result for test {abbreviation}"
            )
        return report[abbreviation]

    # Maps indicator names to FAIR-Checker test abbreviations.
    TEST_ABBREVIATIONS = {
        "unique_ids": "F1A",
        "persistent_ids": "F1B",
        "structured_metadata": "F2A",
        "shared_vocabularies_for_metadata": "F2B",
        "machine_readable_format": "I1",
        "use_shared_ontologies": "I2",
        "external_links": "I3",
        "license_metadata": "R11",
    }

    def unique_ids(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url, branch_hash_or_tag, self.TEST_ABBREVIATIONS["unique_ids"]
        )

    def persistent_ids(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url, branch_hash_or_tag, self.TEST_ABBREVIATIONS["persistent_ids"]
        )

    def structured_metadata(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url, branch_hash_or_tag, self.TEST_ABBREVIATIONS["structured_metadata"]
        )

    def shared_vocabularies_for_metadata(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url,
            branch_hash_or_tag,
            self.TEST_ABBREVIATIONS["shared_vocabularies_for_metadata"],
        )

    def machine_readable_format(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url, branch_hash_or_tag, self.TEST_ABBREVIATIONS["machine_readable_format"]
        )

    def use_shared_ontologies(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url, branch_hash_or_tag, self.TEST_ABBREVIATIONS["use_shared_ontologies"]
        )

    def external_links(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url, branch_hash_or_tag, self.TEST_ABBREVIATIONS["external_links"]
        )

    def license_metadata(self, url, branch_hash_or_tag):
        return self._indicator_checks(
            url, branch_hash_or_tag, self.TEST_ABBREVIATIONS["license_metadata"]
        )
