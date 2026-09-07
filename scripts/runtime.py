#!/usr/bin/env python3
"""Compose lifecycle plus namespace-scoped dynamic LightRAG start/stop."""
import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE_LABEL = "com.amazon.experts.lightrag.namespace"


def read_env(path, private=False):
    path = Path(path).expanduser()
    if private and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("Private configuration must have mode 0600")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError("Expected KEY=value in environment file")
        parsed = shlex.split(value, comments=True)
        values[key] = " ".join(parsed)
    return values


class Runtime:
    def __init__(self, env_files, compose_files=None):
        self.config = read_env(ROOT / "images.env")
        for path in env_files:
            self.config.update(read_env(path, private=True))
        self.namespace = self.config["LIGHTRAG_NAMESPACE"]
        self.config.setdefault("LIGHTRAG_CONTROLLER_TOKEN", self.config.get("LIGHTRAG_API_KEY", ""))
        self.env = {**os.environ, **self.config, "COMPOSE_DISABLE_ENV_FILE": "true"}
        self.command = ["docker", "compose", "--env-file", "/dev/null",
                        "-p", self.config["STACK_NAME"], "-f", str(ROOT / "compose.yaml")]
        for path in compose_files or ():
            path = Path(path).expanduser()
            self.command.extend(["-f", str(path if path.is_absolute() else ROOT / path)])

    def compose(self, *args):
        subprocess.run(self.command + list(args), env=self.env, check=True)

    def metadata(self, kind, name):
        return json.loads(subprocess.check_output(["docker", kind, "inspect", name], text=True))[0]

    def instance_ids(self):
        return subprocess.check_output([
            "docker", "ps", "-aq", "--filter", "label=com.amazon.experts.lightrag.managed=true",
            "--filter", f"label={NAMESPACE_LABEL}={self.namespace}",
        ], text=True).split()

    def up(self):
        ids = self.instance_ids()
        if ids:
            subprocess.run(["docker", "start", *ids], check=True, stdout=subprocess.DEVNULL)
        self.compose("up", "-d", "--wait", "--wait-timeout", "360", "docling", "controller")

    def stop(self, keep_docling=False):
        self.compose("stop", "controller")
        ids = self.instance_ids()
        if ids:
            subprocess.run(["docker", "stop", *ids], check=True, stdout=subprocess.DEVNULL)
        if not keep_docling:
            self.compose("stop", "docling")
        print("Stopped namespace instances and selected services; data volumes remain")

    def status(self):
        self.compose("ps", "-a")
        print(f"Managed LightRAG instances: {len(self.instance_ids())}")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env-file", action="append", required=True,
                        help="Private config; repeat for an explicit override")
    result.add_argument("--compose-file", action="append",
                        help="Additional Compose file, relative to the repository or absolute; repeat to layer")
    return result


def main():
    args_parser = parser()
    args_parser.add_argument("action", choices=["config", "build", "pull", "up", "status", "stop"])
    args_parser.add_argument("--keep-docling", action="store_true",
                             help="On stop, leave shared Docling running")
    args = args_parser.parse_args()
    runtime = Runtime(args.env_file, args.compose_file)
    if args.action == "config":
        runtime.compose("config", "--quiet")
        print("Compose configuration valid")
    elif args.action == "build":
        runtime.compose("build", "controller")
    elif args.action == "pull":
        for key in ("DOCLING_IMAGE", "LIGHTRAG_IMAGE"):
            subprocess.run(["docker", "pull", runtime.config[key]], check=True)
    elif args.action == "stop":
        runtime.stop(args.keep_docling)
    else:
        getattr(runtime, args.action)()


if __name__ == "__main__":
    main()
