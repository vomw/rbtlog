#!/usr/bin/env python3
# encoding: utf-8
# SPDX-FileCopyrightText: 2026 Sylvia van Os <sylvia@hackerchick.me>
# SPDX-License-Identifier: AGPL-3.0-or-later

import argparse
import json
import os
import subprocess
import sys
import tempfile

class Error(Exception):
    """Base class for errors."""


def load_modules() -> Dict[Any, Any]:
    """Load modules.json."""
    with open("modules.json", encoding="utf-8") as fh:
        return json.load(fh)        # type: ignore[no-any-return]


def save_modules(data: Dict[Any, Any]) -> None:
    """Save modules.json."""
    with open("modules.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


# Taken from build.py
def run_command(*args: str, cwd: Optional[str] = None, verbose: bool = False) -> str:
    r"""
    Run command and capture stdout + stderr.

    >>> run_command("echo", "OK")
    'OK\n'
    >>> run_command("bash", "-c", "echo OK >&2")
    'OK\n'
    >>> try:
    ...     run_command("false")
    ... except subprocess.CalledProcessError as e:
    ...     print(e)
    Command '('false',)' returned non-zero exit status 1.

    """
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT).stdout.decode()


def update_modules(*, verbose: bool = False) -> None:
    """Update modules.json."""
    modules = load_modules()
    modified = False
    repositories = {}
    for name, module in modules.items():
        module_modified = False
        if f"{module['repository']}_{module['branch']}" not in repositories:
            tmpdir = tempfile.TemporaryDirectory()
            repositories[f"{module['repository']}_{module['branch']}"] = tmpdir
            run_command("git", "clone", "-b", module["branch"], module["repository"], tmpdir.name, verbose=verbose)
        commit = run_command("git", "-C", repositories[f"{module['repository']}_{module['branch']}"].name, "rev-parse", "HEAD", verbose=verbose).strip()
        if modules[name]['commit'] != commit:
            modules[name]['commit'] = commit
            module_modified = True
        remote_module_info_path = os.path.join(tmpdir.name, "modules.json")
        if not os.path.isfile(remote_module_info_path):
            print(f"Failed to update module {name}: branch {module["branch"]} of repository {module["repository"]} lacks modules.json file.")
            continue
        try:
            with open(remote_module_info_path, "r") as f:
                module_info = json.load(f)
                newest_version = module_info[module['name']]["latest_version"]
                if modules[name]['version'] != newest_version:
                    modules[name]['version'] = newest_version
                    module_modified = True
        except Exception as e:
            print(f"Failed to update module {name}: {e}", file=sys.stderr)
        if module_modified:
            if verbose:
                print(f"Module {name} updated to version {module['version']} and commit {module['commit']}.", file=sys.stderr)

            modified = True
    for tmpdir in repositories.values():
        tmpdir.cleanup()
    if modified:
        save_modules(modules)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="update modules.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    update_modules(verbose=args.verbose)

# vim: set tw=80 sw=4 sts=4 et fdm=marker :
