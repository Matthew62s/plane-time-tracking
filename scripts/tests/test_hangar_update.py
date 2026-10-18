# Copyright (c) 2026-present Maciej Szymczak and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "hangar_update", Path(__file__).parents[1] / "hangar-update.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

WRAPPER = (
    "#!/bin/sh\nexec docker compose --project-directory /opt/hangar/runtime -p hangar "
    "--env-file /opt/hangar/runtime/plane.env -f /opt/hangar/runtime/compose.yml "
    "-f /opt/hangar/runtime/compose.caddy.yml -f /opt/hangar/runtime/compose.smtp.yml "
    '-f /opt/hangar/runtime/compose.time-report.yml "$@"\n'
)


class FakeMachine:
    """Model command boundaries and live image state; never contacts a real Docker daemon."""

    def __init__(self, updater):
        self.updater = updater
        self.calls = []
        self.config = {
            name: {"image": f"old/{name}:before"}
            for name in [
                "api",
                "web",
                "mail-worker",
                "worker",
                "migrator",
                "plane-db",
                "plane-redis",
            ]
        }
        self.config["api"]["environment"] = {
            "WEB_URL": "https://hangar.example",
            "SECRET_KEY": "never-print-this",
        }
        self.active = {"api", "web", "worker", "mail-worker", "plane-db", "plane-redis"}
        self.images = {name: f"sha256:old-{name}" for name in self.active}
        self.tags = {}
        self.fail = None
        self.activation_failed = False

    def __call__(self, args, *, output=None, input_file=None, check=True, cwd=None):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        stdout = b""
        code = 0
        if args[0] == str(self.updater.wrapper):
            command = args[1:]
            candidate = None
            if command[:1] == ["-f"]:
                candidate = Path(command[1])
                command = command[2:]
            config = json.loads(json.dumps(self.config))
            if self.updater.override.exists():
                for name, data in json.loads(self.updater.override.read_text())[
                    "services"
                ].items():
                    config[name].update(data)
            if candidate:
                for name, data in json.loads(candidate.read_text())["services"].items():
                    config[name].update(data)
            if command[:1] == ["config"]:
                stdout = json.dumps({"services": config}).encode()
            elif command[:1] == ["ps"]:
                if "--services" in command:
                    stdout = "\n".join(sorted(self.active)).encode()
                else:
                    service = command[-1]
                    stdout = service.encode() if service in self.active else b""
            elif command[:1] == ["run"]:
                code = 1 if self.fail == "migrations" else 0
            elif command[:1] == ["up"]:
                if self.fail == "rollback" or (
                    self.fail == "activation" and not self.activation_failed
                ):
                    self.activation_failed = True
                    raise module.UpdateError("activation failed")
                start = command.index("--force-recreate") + 1
                for service in command[start:]:
                    self.images[service] = self.tags[config[service]["image"]]
        elif args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir()
        elif args[0] == "git" and "rev-parse" in args:
            stdout = b"a" * 40
        elif args[:2] == ["docker", "build"]:
            if self.fail == "build":
                raise module.UpdateError("build failed")
            self.tags[args[args.index("-t") + 1]] = (
                "sha256:new-" + args[args.index("-t") + 1].split("/")[1].split(":")[0]
            )
        elif args[:2] == ["docker", "exec"]:
            if "pg_restore" in args:
                assert Path(input_file).read_bytes() == b"PGDMPtest-backup"
                if self.fail == "archive":
                    raise module.UpdateError("invalid archive")
                stdout = b"archive contents"
            elif "pg_dump" in args[-1]:
                if self.fail == "backup":
                    raise module.UpdateError("password rejected")
                stdout = b"" if self.fail == "empty_backup" else b"PGDMPtest-backup"
        elif args[:2] == ["docker", "tag"]:
            self.tags[args[3]] = self.tags.get(args[2], args[2])
        elif args[:2] == ["docker", "inspect"]:
            service = args[-1]
            if "--format" in args:
                stdout = self.images[service].encode()
            else:
                stdout = json.dumps(
                    [{"Image": self.images[service], "State": {"Running": True}}]
                ).encode()
        elif args[:3] == ["docker", "image", "inspect"]:
            if args[-1] not in self.tags:
                raise module.UpdateError("image missing")
            stdout = self.tags[args[-1]].encode()
        if output:
            with Path(output).open("ab") as log:
                log.write(stdout)
        if check and code:
            raise module.UpdateError("command failed")
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=b"")


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.updater = module.Updater(Path(self.temp.name) / "hangar")
        self.updater.runtime.mkdir(parents=True)
        self.updater.wrapper.write_text(WRAPPER)
        self.updater.wrapper.chmod(0o755)
        (self.updater.runtime / "plane.env").write_text("SECRET_KEY=never-print-this\n")
        self.machine = FakeMachine(self.updater)
        self.updater.run = self.machine
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        patch("sys.stdout", self.output).start()
        patch("sys.stderr", self.output).start()
        patch.object(module, "urlopen", self.response).start()

    @staticmethod
    @contextlib.contextmanager
    def response(url, timeout):
        response = io.BytesIO(
            b'{"instance": {}}'
            if url.endswith("/api/instances/")
            else b"<html>Hangar</html>"
        )
        response.status = 200
        yield response

    def update(self):
        with self.updater.lock():
            self.updater.update(["api", "web"], "preview", None)

    def test_update_backup_images_workers_and_smtp(self):
        self.update()
        directory, manifest = self.updater.latest()
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(
            (directory / "database.dump").read_bytes(), b"PGDMPtest-backup"
        )
        self.assertEqual(
            (directory / "runtime" / "hangar-compose").read_text(), WRAPPER
        )
        self.assertIn("compose.smtp.yml", self.updater.wrapper.read_text())
        self.assertIn("compose.time-report.yml", self.updater.wrapper.read_text())
        self.assertIn(str(self.updater.override), self.updater.wrapper.read_text())
        self.assertEqual(self.updater.wrapper.stat().st_mode & 0o777, 0o755)
        override = json.loads(self.updater.override.read_text())["services"]
        self.assertEqual(override["api"], override["mail-worker"])
        self.assertEqual(override["api"], override["worker"])
        self.assertEqual(override["api"], override["migrator"])
        self.assertNotIn("plane-db", override)
        self.assertNotIn("plane-redis", override)
        self.assertNotIn("never-print-this", self.output.getvalue())
        self.assertEqual(self.machine.images["plane-db"], "sha256:old-plane-db")

    def test_build_failure_preserves_running_services(self):
        self.machine.fail = "build"
        with self.assertRaises(module.UpdateError):
            self.update()
        self.assertFalse((self.updater.state / "latest").exists())
        self.assertEqual(self.updater.wrapper.read_text(), WRAPPER)
        self.assertFalse(self.updater.override.exists())

    def test_pending_migrations_do_not_back_up_or_activate(self):
        self.machine.fail = "migrations"
        with self.assertRaisesRegex(module.UpdateError, "Migration readiness"):
            self.update()
        self.assertFalse(any("pg_dump" in c[-1] for c in self.machine.calls))
        self.assertEqual(self.machine.images["api"], "sha256:old-api")
        self.assertFalse(self.updater.override.exists())

    def test_backup_failures_prevent_activation(self):
        for failure in ["backup", "archive", "empty_backup"]:
            with self.subTest(failure=failure):
                self.machine.fail = failure
                # Fresh record names are normally unique between CLI invocations.
                with (
                    patch.object(
                        module.os,
                        "getpid",
                        return_value={"backup": 1, "archive": 2, "empty_backup": 3}[
                            failure
                        ],
                    ),
                    self.assertRaises(module.UpdateError),
                ):
                    self.update()
                self.assertEqual(self.machine.images["api"], "sha256:old-api")
                self.assertFalse(self.updater.override.exists())

    def test_activation_failure_restores_pinned_previous_images(self):
        self.machine.fail = "activation"
        with self.assertRaisesRegex(module.UpdateError, "previous images restored"):
            self.update()
        _, manifest = self.updater.latest()
        self.assertEqual(manifest["status"], "rolled_back")
        self.assertEqual(self.machine.images["api"], "sha256:old-api")
        self.assertIn("compose.smtp.yml", self.updater.wrapper.read_text())

    def test_readiness_failure_restores_previous_images(self):
        original = self.updater.wait
        calls = []

        def fail_once(*args):
            calls.append(args)
            if len(calls) == 1:
                raise module.UpdateError("readiness timeout")
            return original(*args)

        self.updater.wait = fail_once
        with self.assertRaisesRegex(module.UpdateError, "previous images restored"):
            self.update()
        self.assertEqual(self.machine.images["api"], "sha256:old-api")

    def test_manual_rollback_persists_across_sessions(self):
        self.update()
        other = module.Updater(self.updater.root, self.machine)
        directory, manifest = other.latest()
        other.restore(directory, manifest)
        self.assertEqual(read_status(directory), "rolled_back")
        self.assertEqual(self.machine.images["api"], "sha256:old-api")
        self.assertEqual(self.machine.images["web"], "sha256:old-web")

    def test_missing_rollback_image_does_not_change_current_configuration(self):
        self.update()
        directory, manifest = self.updater.latest()
        before = self.updater.override.read_text()
        self.machine.tags.pop(manifest["before_images"]["api"])
        with self.assertRaisesRegex(module.UpdateError, "image missing"):
            self.updater.restore(directory, manifest)
        self.assertEqual(self.updater.override.read_text(), before)
        self.assertEqual(self.machine.images["api"], "sha256:new-api")

    def test_existing_unselected_override_preserved(self):
        self.machine.config["admin"] = {"image": "old/admin:original"}
        module.write_json(
            self.updater.override,
            {"services": {"admin": {"image": "local/admin:custom"}}},
        )
        self.update()
        self.assertEqual(
            json.loads(self.updater.override.read_text())["services"]["admin"]["image"],
            "local/admin:custom",
        )

    def test_lock_blocks_concurrent_updates(self):
        with (
            self.updater.lock(),
            self.assertRaisesRegex(module.UpdateError, "Another update"),
            module.Updater(self.updater.root).lock(),
        ):
            pass

    def test_plan_changes_no_containers_or_wrapper(self):
        with self.updater.lock():
            self.updater.plan(["api", "web"])
        self.assertEqual(self.updater.wrapper.read_text(), WRAPPER)
        self.assertTrue(
            all(command[1] in {"config", "ps"} for command in self.machine.calls)
        )

    def test_no_arbitrary_branch_or_unmerged_ref(self):
        with self.assertRaisesRegex(module.UpdateError, "full commit"):
            self.updater.build(Path(self.temp.name), {"api"}, "feature/unreviewed")
        self.assertFalse(self.machine.calls)

    def test_failed_rollback_is_recorded_for_recovery(self):
        self.machine.fail = "rollback"
        with self.assertRaisesRegex(module.UpdateError, "Automatic rollback failed"):
            self.update()
        _, manifest = self.updater.latest()
        self.assertEqual(manifest["status"], "rollback_failed")

    def test_html_fallback_is_not_api_readiness(self):
        self.machine.tags["expected"] = self.machine.images["api"]

        @contextlib.contextmanager
        def html_only(url, timeout):
            response = io.BytesIO(b"<html>SPA fallback</html>")
            response.status = 200
            yield response

        with (
            patch.object(module, "urlopen", html_only),
            patch.object(module.time, "monotonic", side_effect=[0, 0, 181]),
            patch.object(module.time, "sleep"),
            self.assertRaisesRegex(module.UpdateError, "Readiness timed out"),
        ):
            self.updater.wait(["api"], {"api": "expected"}, "https://hangar.example")

    def test_missing_replicas_do_not_pass_readiness(self):
        self.machine.tags["expected"] = self.machine.images["api"]
        self.machine.config["api"]["deploy"] = {"replicas": 2}
        with (
            patch.object(module.time, "monotonic", side_effect=[0, 0, 181]),
            patch.object(module.time, "sleep"),
            self.assertRaisesRegex(module.UpdateError, "Readiness timed out"),
        ):
            self.updater.wait(["api"], {"api": "expected"}, "https://hangar.example")

    def test_wrapper_attachment_is_idempotent_and_quoted(self):
        path = Path("/opt/hangar with spaces/runtime/compose.hangar-update.json")
        once = module.attach_override(WRAPPER, path)
        self.assertEqual(module.attach_override(once, path), once)
        self.assertIn(f"-f '{path}'", once)
        with self.assertRaises(module.UpdateError):
            module.attach_override('#!/bin/sh\ncustom-wrapper "$@"\n', path)

    def test_url_rejects_credentials_and_nonlocal_plaintext(self):
        for value in [
            "",
            "https://name:secret@hangar.example",
            "http://hangar.example",
            "https://hangar.example/path",
        ]:
            with self.subTest(value=value), self.assertRaises(module.UpdateError):
                module.validate_url(value)
        self.assertEqual(
            module.validate_url("https://hangar.example/"), "https://hangar.example"
        )

    def test_command_failure_does_not_print_captured_secrets(self):
        result = subprocess.CompletedProcess(
            ["docker"], 1, stdout=b"secret", stderr=b"password"
        )
        with (
            patch.object(module.subprocess, "run", return_value=result),
            self.assertRaises(module.UpdateError) as error,
        ):
            module.Updater.command(["docker", "config"])
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("password", str(error.exception))


def read_status(directory):
    return json.loads((directory / "manifest.json").read_text())["status"]


if __name__ == "__main__":
    unittest.main()
