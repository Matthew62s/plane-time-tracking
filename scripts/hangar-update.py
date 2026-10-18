#!/usr/bin/env python3
# Copyright (c) 2026-present Maciej Szymczak and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Update the existing Hangar Compose installation without replacing its settings.

Requires Python 3.10+, Git, Docker Compose v2 and the site's hangar-compose wrapper.
No production operation is performed on import. See docs/server-updates.md.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

REPOSITORY = "https://github.com/Matthew62s/plane-time-tracking.git"
COMPONENTS = {
    "api": ("apps/api", "Dockerfile.api"),
    "web": (".", "apps/web/Dockerfile.web"),
    "admin": (".", "apps/admin/Dockerfile.admin"),
    "space": (".", "apps/space/Dockerfile.space"),
    "live": (".", "apps/live/Dockerfile.live"),
    "proxy": ("apps/proxy", "Dockerfile.ce"),
}
API_SERVICES = {
    "api",
    "worker",
    "mail-worker",
    "beat-worker",
    "import-worker",
    "migrator",
}


class UpdateError(Exception):
    pass


def atomic_text(path, text, mode=0o600):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as output:
        os.chmod(tmp, mode)
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    atomic_text(path, json.dumps(value, indent=2) + "\n")


def attach_override(wrapper, override):
    """Only extend the known simple wrapper; preserve every existing flag."""
    lines = [
        line
        for line in wrapper.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if (
        len(lines) != 1
        or not lines[0].startswith("exec docker compose ")
        or not lines[0].endswith(' "$@"')
    ):
        raise UpdateError(
            "Unsupported wrapper. Expected one 'exec docker compose ... \"$@\"' line."
        )
    # Installation paths are passed to a shell wrapper; quote them, not interpolate unescaped text.
    import shlex

    tokens = shlex.split(lines[0])
    if any(
        token == "-f" and index + 1 < len(tokens) and tokens[index + 1] == str(override)
        for index, token in enumerate(tokens)
    ):
        return wrapper
    return wrapper.replace(' "$@"', f' -f {shlex.quote(str(override))} "$@"')


def service_component(service):
    return (
        "api" if service in API_SERVICES else service if service in COMPONENTS else None
    )


def validate_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise UpdateError(
            "Readiness URL must be a site origin, without credentials, path or query."
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise UpdateError("Use HTTPS for a non-local readiness URL.")
    return value.rstrip("/")


class Updater:
    def __init__(self, root, runner=None):
        self.root = Path(root).resolve()
        self.runtime = self.root / "runtime"
        self.wrapper = self.runtime / "hangar-compose"
        self.override = self.runtime / "compose.hangar-update.json"
        self.state = self.root / "deploy-state"
        self.run = runner or self.command

    @staticmethod
    def command(args, *, output=None, input_file=None, check=True, cwd=None):
        # Never print captured config: it contains the installation's secrets.
        with contextlib.ExitStack() as stack:
            destination = (
                stack.enter_context(Path(output).open("ab"))
                if output
                else subprocess.PIPE
            )
            source = (
                stack.enter_context(Path(input_file).open("rb")) if input_file else None
            )
            result = subprocess.run(
                [str(a) for a in args],
                check=False,
                cwd=cwd,
                stdin=source,
                stdout=destination,
                stderr=destination if output else subprocess.PIPE,
                timeout=7200,
            )
        if check and result.returncode:
            raise UpdateError(
                f"Command failed ({result.returncode}): {args[0]} {args[1] if len(args) > 1 else ''}."
                + (
                    f" See {output}."
                    if output
                    else " Output withheld to protect configuration values."
                )
            )
        return result

    def compose(self, *args, candidate=None, **kwargs):
        command = [self.wrapper]
        if candidate:
            command += ["-f", candidate]
        return self.run(command + list(args), **kwargs)

    def config(self, candidate=None):
        result = self.compose("config", "--format", "json", candidate=candidate)
        return json.loads(result.stdout)["services"]

    def running(self):
        return set(
            self.compose("ps", "--status", "running", "--services")
            .stdout.decode()
            .split()
        )

    @contextlib.contextmanager
    def lock(self):
        if not self.wrapper.is_file() or not os.access(self.wrapper, os.X_OK):
            raise UpdateError(f"Executable wrapper required: {self.wrapper}")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state / "lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpdateError("Another update/rollback is running.")
            yield

    def selection(self, components):
        config = self.config()
        selected = {
            name: service_component(name)
            for name in config
            if service_component(name) in components
        }
        if not selected or "api" not in config or "web" not in config:
            raise UpdateError(
                "Expected the existing Hangar installation with api and web services."
            )
        running = self.running()
        active = sorted(set(selected) & running)
        if not active:
            raise UpdateError(
                "No selected application services are running; refusing to bootstrap a new installation."
            )
        return config, selected, active

    def plan(self, components):
        _, selected, active = self.selection(components)
        attach_override(self.wrapper.read_text(), self.override)
        print("Build components:", ", ".join(sorted(set(selected.values()))))
        print("Recreate running services:", ", ".join(active))
        print("Database, Redis, RabbitMQ and storage services are not recreated.")
        print("Migration changes stop the update before activation.")

    def backup(self, directory, active, config):
        print(
            "Backing up runtime configuration, database and current images...",
            flush=True,
        )
        shutil.copytree(self.runtime, directory / "runtime")
        dbs = (
            self.compose("ps", "--status", "running", "--quiet", "plane-db")
            .stdout.decode()
            .split()
        )
        if len(dbs) != 1:
            raise UpdateError("Expected exactly one running plane-db container.")
        dump = directory / "database.dump"
        # Authentication remains inside the container. -w prevents an interactive prompt.
        self.run(
            [
                "docker",
                "exec",
                dbs[0],
                "sh",
                "-ec",
                (
                    'export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing}"; '
                    'exec pg_dump -w -h 127.0.0.1 -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -Fc'
                ),
            ],
            output=dump,
        )
        if not dump.is_file() or dump.stat().st_size == 0:
            raise UpdateError("Empty database backup; no containers were changed.")
        # Validate the archive using the database image's matching pg_restore.
        self.run(
            ["docker", "exec", "-i", dbs[0], "pg_restore", "--list"],
            input_file=dump,
            output=directory / "database-contents.txt",
        )
        before = {}
        for service in active:
            ids = (
                self.compose("ps", "--status", "running", "--quiet", service)
                .stdout.decode()
                .split()
            )
            if not ids:
                raise UpdateError(f"Service stopped during preparation: {service}")
            images = {
                self.run(["docker", "inspect", "--format", "{{.Image}}", ident])
                .stdout.decode()
                .strip()
                for ident in ids
            }
            if len(images) != 1:
                raise UpdateError(
                    f"Mixed images in replicas for {service}; resolve before updating."
                )
            tag = f"hangar-local/rollback:{directory.name}-{service}"
            self.run(["docker", "tag", images.pop(), tag])
            before[service] = tag
        return before

    def build(self, directory, components, ref):
        if ref != "preview" and not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise UpdateError(
                "Use preview or a full commit SHA already merged into preview."
            )
        source = directory / "source"
        log = directory / "build.log"
        print(f"Fetching source; build output: {log}", flush=True)
        self.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "--single-branch",
                "--branch",
                "preview",
                REPOSITORY,
                source,
            ],
            output=log,
        )
        revision = (
            self.run(
                [
                    "git",
                    "-C",
                    source,
                    "rev-parse",
                    "origin/preview" if ref == "preview" else ref,
                ]
            )
            .stdout.decode()
            .strip()
        )
        self.run(
            [
                "git",
                "-C",
                source,
                "merge-base",
                "--is-ancestor",
                revision,
                "origin/preview",
            ]
        )
        self.run(["git", "-C", source, "checkout", "--detach", revision], output=log)
        images = {}
        for component in sorted(components):
            context, dockerfile = COMPONENTS[component]
            tag = f"hangar-local/{component}:{directory.name}-{revision[:12]}"
            print(f"Building {component} ({revision[:12]})...", flush=True)
            self.run(
                [
                    "docker",
                    "build",
                    "--label",
                    f"org.opencontainers.image.revision={revision}",
                    "--build-arg",
                    f"HANGAR_SOURCE_REVISION={revision}",
                    "--build-arg",
                    f"VITE_SOURCE_CODE_URL={REPOSITORY.removesuffix('.git')}/tree/{revision}",
                    "-f",
                    source / context / dockerfile,
                    "-t",
                    tag,
                    source / context,
                ],
                output=log,
            )
            images[component] = tag
        return revision, images

    def migrations(self, candidate, selected):
        if "api" in selected:
            result = self.compose(
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "--entrypoint",
                "python",
                "api",
                "manage.py",
                "migrate",
                "--check",
                candidate=candidate,
                check=False,
            )
            if result.returncode:
                raise UpdateError(
                    "Migration readiness check failed. Pending migrations or a backend error require "
                    "a separate rollout plan; no application containers were replaced."
                )

    def activate(self, images):
        wrapper = attach_override(self.wrapper.read_text(), self.override)
        write_json(
            self.override,
            {
                "services": {
                    service: {"image": image} for service, image in images.items()
                }
            },
        )
        atomic_text(self.wrapper, wrapper, self.wrapper.stat().st_mode & 0o777)

    def wait(self, services, expected, url, timeout=180):
        config = self.config()
        counts = {
            service: int(
                config[service].get(
                    "scale", config[service].get("deploy", {}).get("replicas", 1)
                )
            )
            for service in services
        }
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready = True
            for service in services:
                ids = (
                    self.compose("ps", "--status", "running", "--quiet", service)
                    .stdout.decode()
                    .split()
                )
                if len(ids) != counts[service]:
                    ready = False
                desired = (
                    self.run(
                        [
                            "docker",
                            "image",
                            "inspect",
                            "--format",
                            "{{.Id}}",
                            expected[service],
                        ]
                    )
                    .stdout.decode()
                    .strip()
                )
                for ident in ids:
                    data = json.loads(self.run(["docker", "inspect", ident]).stdout)[0]
                    ready &= (
                        data["Image"] == desired
                        and data["State"].get("Running", False)
                        and not data["State"].get("Restarting", False)
                        and data["State"].get("Health", {}).get("Status", "healthy")
                        == "healthy"
                    )
            if ready:
                try:
                    with urlopen(url + "/", timeout=10) as response:
                        ready = (
                            response.status == 200
                            and b"<html" in response.read(65536).lower()
                        )
                    with urlopen(url + "/api/instances/", timeout=10) as response:
                        ready &= response.status == 200 and isinstance(
                            json.loads(response.read(1048576)), dict
                        )
                except (OSError, ValueError):
                    ready = False
            if ready:
                return
            time.sleep(3)
        raise UpdateError(
            "Readiness timed out (container images, web HTML and API JSON)."
        )

    def restore(self, directory, manifest):
        # Restore previous image selection, preserving the site's wrapper, SMTP overrides,
        # volumes and infrastructure. No automatic schema downgrade or database restore.
        before = manifest["before_images"]
        for service in manifest["active"]:
            self.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", before[service]]
            )
        self.activate(before)
        self.compose(
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            *manifest["active"],
        )
        self.wait(manifest["active"], before, manifest["url"])
        manifest["status"] = "rolled_back"
        write_json(directory / "manifest.json", manifest)

    def update(self, components, ref, url):
        config, selected, active = self.selection(components)
        attach_override(self.wrapper.read_text(), self.override)
        environment = config["api"].get("environment", {})
        url = validate_url(
            url or environment.get("APP_BASE_URL") or environment.get("WEB_URL") or ""
        )
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"  # noqa: UP017
        directory = self.state / name
        directory.mkdir(mode=0o700)
        revision, images = self.build(directory, set(selected.values()), ref)
        # Carry forward any previous managed overrides for components not selected now.
        previous = (
            read_json(self.override).get("services", {})
            if self.override.exists()
            else {}
        )
        after = {name: value["image"] for name, value in previous.items()}
        after.update({name: images[component] for name, component in selected.items()})
        candidate = directory / "candidate.json"
        write_json(
            candidate,
            {"services": {name: {"image": image} for name, image in after.items()}},
        )
        candidate_config = self.config(candidate)
        if any(
            candidate_config[name].get("image") != image
            for name, image in after.items()
        ):
            raise UpdateError("Compose did not select the new images.")
        self.migrations(candidate, selected)
        pinned = self.backup(directory, active, config)
        before = {name: value["image"] for name, value in previous.items()}
        before.update({name: config[name]["image"] for name in selected})
        before.update(pinned)
        manifest = {
            "revision": revision,
            "url": url,
            "active": active,
            "before_images": before,
            "after_images": after,
            "status": "prepared",
        }
        write_json(directory / "manifest.json", manifest)
        atomic_text(self.state / "latest", name + "\n")
        print(f"Backup verified: {directory}. Activating...", flush=True)
        try:
            manifest["status"] = "activating"
            write_json(directory / "manifest.json", manifest)
            self.activate(after)
            self.compose(
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                *active,
            )
            self.wait(active, after, url)
        except (UpdateError, OSError, KeyboardInterrupt) as error:
            print(
                "Activation failed; restoring previous images...",
                file=sys.stderr,
                flush=True,
            )
            try:
                self.restore(directory, manifest)
            except (UpdateError, OSError, KeyboardInterrupt):
                manifest["status"] = "rollback_failed"
                write_json(directory / "manifest.json", manifest)
                raise UpdateError(
                    f"Automatic rollback failed. Preserve {directory}; inspect services with the wrapper."
                ) from error
            raise UpdateError(
                "Update failed; previous images restored. Database was not restored or migrated."
            ) from error
        manifest["status"] = "succeeded"
        write_json(directory / "manifest.json", manifest)
        print(
            f"Updated to {revision}. Backup: {directory}\nCheck the application under your account; rollback: hangar-update rollback"
        )

    def latest(self):
        if not (self.state / "latest").is_file():
            raise UpdateError(
                "No update has reached activation; there is nothing to roll back."
            )
        name = (self.state / "latest").read_text().strip()
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9]+", name):
            raise UpdateError("Invalid deployment record.")
        directory = self.state / name
        return directory, read_json(directory / "manifest.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/hangar", type=Path)
    parser.add_argument("action", choices=["plan", "update", "rollback", "status"])
    parser.add_argument(
        "--components", nargs="+", choices=list(COMPONENTS), default=list(COMPONENTS)
    )
    parser.add_argument("--ref", default="preview")
    parser.add_argument(
        "--url",
        help="Public site origin; default is APP_BASE_URL/WEB_URL in the existing API config",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    updater = Updater(args.root)
    try:
        with updater.lock():
            if args.action == "plan":
                updater.plan(args.components)
            elif args.action == "update":
                updater.update(args.components, args.ref, args.url)
            else:
                directory, manifest = updater.latest()
                if args.action == "status":
                    print(
                        json.dumps(
                            {
                                "revision": manifest["revision"],
                                "status": manifest["status"],
                                "backup": str(directory),
                            },
                            indent=2,
                        )
                    )
                elif manifest["status"] == "rolled_back":
                    print("Previous images have already been restored.")
                else:
                    updater.restore(directory, manifest)
                    print("Previous images restored; database untouched.")
    except (UpdateError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Update stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
