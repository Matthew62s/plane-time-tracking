# Copyright (c) 2026-present Maciej Szymczak and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Opt-in integration test against disposable containers, never the host installation."""

import importlib.util
import os
import secrets
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

SPEC = importlib.util.spec_from_file_location(
    "updater_docker", Path(__file__).parents[1] / "hangar-update.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

SERVER = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = b'{"instance": {}}' if self.path == '/api/instances/' else b'<html>Hangar smoke</html>'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json' if self.path.startswith('/api/') else 'text/html')
        self.end_headers()
        self.wfile.write(data)
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""


@unittest.skipUnless(
    os.environ.get("HANGAR_UPDATE_DOCKER_TEST") == "1",
    "Set HANGAR_UPDATE_DOCKER_TEST=1 for disposable Docker integration",
)
class DockerUpdateTest(unittest.TestCase):
    def run_command(self, args, cwd=None, check=True):
        return subprocess.run(
            [str(a) for a in args],
            cwd=cwd,
            check=check,
            capture_output=True,
            timeout=300,
        )

    def test_real_backup_update_migration_guard_and_rollback(self):
        with tempfile.TemporaryDirectory(
            prefix="hangar-updater-integration-"
        ) as temporary:
            root = Path(temporary)
            source = root / "repository"
            api = source / "apps/api"
            web = source / "apps/web"
            api.mkdir(parents=True)
            web.mkdir(parents=True)
            (api / "server.py").write_text(SERVER)
            (api / "manage.py").write_text("raise SystemExit(0)\n")
            (api / "Dockerfile.api").write_text(
                'FROM python:3.12-alpine\nWORKDIR /code\nCOPY . .\nCMD ["python", "server.py"]\n'
            )
            (web / "Dockerfile.web").write_text(
                'FROM python:3.12-alpine\nWORKDIR /code\nCOPY apps/api/server.py .\nCMD ["python", "server.py"]\n'
            )
            self.run_command(["git", "init", "-b", "preview", source])
            self.run_command(["git", "add", "."], cwd=source)
            commit = [
                "git",
                "-c",
                "user.name=Updater test",
                "-c",
                "user.email=updater@example.invalid",
                "commit",
                "-m",
                "fixture",
            ]
            self.run_command(commit, cwd=source)
            project = "hangar-updater-test-" + secrets.token_hex(4)
            baseline = project + ":baseline"
            self.run_command(
                ["docker", "build", "-f", api / "Dockerfile.api", "-t", baseline, api]
            )
            installation = root / "installation"
            runtime = installation / "runtime"
            runtime.mkdir(parents=True)
            config = {
                "services": {
                    "plane-db": {
                        "image": "postgres:15.7-alpine",
                        "environment": {
                            "POSTGRES_USER": "plane",
                            "POSTGRES_DB": "plane",
                            "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
                        },
                        "tmpfs": ["/var/lib/postgresql/data"],
                    },
                    "api": {"image": baseline},
                    "worker": {
                        "image": baseline,
                        "command": ["python", "-c", "import time; time.sleep(3600)"],
                    },
                    "web": {"image": baseline, "ports": ["127.0.0.1::8080"]},
                }
            }
            module.write_json(runtime / "compose.json", config)
            module.write_json(
                runtime / "smtp.json",
                {
                    "services": {
                        "api": {"environment": {"EMAIL_HOST": "retained-smtp.invalid"}}
                    }
                },
            )
            wrapper = runtime / "hangar-compose"
            wrapper.write_text(
                f'#!/bin/sh\nexec docker compose -p {project} -f {shlex.quote(str(runtime / "compose.json"))} -f {shlex.quote(str(runtime / "smtp.json"))} "$@"\n'
            )
            wrapper.chmod(0o755)
            updater = module.Updater(installation)
            try:
                updater.compose("up", "-d")
                db = (
                    updater.compose("ps", "--quiet", "plane-db").stdout.decode().strip()
                )
                for _ in range(60):
                    if (
                        self.run_command(
                            ["docker", "exec", db, "pg_isready", "-U", "plane"],
                            check=False,
                        ).returncode
                        == 0
                    ):
                        break
                    time.sleep(1)
                else:
                    self.fail("Disposable PostgreSQL did not start")
                address = updater.compose("port", "web", "8080").stdout.decode().strip()
                origin = "http://" + address

                # The cloud Docker daemon has its own network namespace: published
                # loopback ports are on that host, not this Python process. Resolve
                # the current fixture container for each real HTTP request instead.
                def fixture_http(url, timeout):
                    web_id = (
                        updater.compose("ps", "--quiet", "web").stdout.decode().strip()
                    )
                    ip = (
                        self.run_command(
                            [
                                "docker",
                                "inspect",
                                "--format",
                                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                                web_id,
                            ]
                        )
                        .stdout.decode()
                        .strip()
                    )
                    return build_opener(ProxyHandler({})).open(
                        url.replace(origin, "http://" + ip + ":8080"), timeout=timeout
                    )

                http_patch = patch.object(module, "urlopen", fixture_http)
                http_patch.start()
                self.addCleanup(http_patch.stop)
                before = updater.compose("ps", "--quiet", "api").stdout.decode().strip()
                with patch.object(module, "REPOSITORY", str(source)), updater.lock():
                    updater.update(["api", "web"], "preview", origin)
                directory, manifest = updater.latest()
                self.assertEqual(manifest["status"], "succeeded")
                self.assertGreater((directory / "database.dump").stat().st_size, 100)
                self.assertEqual(
                    updater.config()["api"]["environment"]["EMAIL_HOST"],
                    "retained-smtp.invalid",
                )
                self.assertNotEqual(
                    updater.compose("ps", "--quiet", "api").stdout.decode().strip(),
                    before,
                )
                with updater.lock():
                    updater.restore(directory, manifest)
                self.assertEqual(
                    module.read_json(directory / "manifest.json")["status"],
                    "rolled_back",
                )
                previous_override = updater.override.read_text()
                (api / "manage.py").write_text("raise SystemExit(1)\n")
                self.run_command(["git", "add", "."], cwd=source)
                self.run_command(commit, cwd=source)
                with (
                    patch.object(module, "REPOSITORY", str(source)),
                    updater.lock(),
                    self.assertRaisesRegex(module.UpdateError, "Migration readiness"),
                ):
                    updater.update(["api", "web"], "preview", origin)
                self.assertEqual(updater.override.read_text(), previous_override)
            finally:
                # Only the uniquely named disposable stack created above is removed.
                updater.compose("down", "--volumes", "--remove-orphans", check=False)
                self.run_command(["docker", "image", "rm", baseline], check=False)


if __name__ == "__main__":
    unittest.main()
