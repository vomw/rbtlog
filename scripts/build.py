#!/usr/bin/env python3
# encoding: utf-8
# SPDX-FileCopyrightText: 2025 FC (Fay) Stegerman <flx@obfusk.net>
# SPDX-FileCopyrightText: 2025-2026 Sylvia van Os <sylvia@hackerchick.me>
# SPDX-License-Identifier: AGPL-3.0-or-later

import argparse
import copy
import dataclasses
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from dataclasses import dataclass
from enum import Enum
from functools import reduce
from typing import Any, Dict, List, Optional, Tuple

import apksigcopier
import jsonschema
import repro_apk.binres as binres
import requests

from ruamel.yaml import YAML

BuildBackend = Enum("BuildBackend", ["PODMAN", "DOCKER"])   # FIXME

NOAPK = "none"
NOTAG = ":commit:"

PROVISION_ROOT_TIMEOUT = int(os.environ.get("RBTLOG_PROVISION_ROOT_TIMEOUT") or 10)
PROVISION_TIMEOUT = int(os.environ.get("RBTLOG_PROVISION_TIMEOUT") or 10)
BUILD_TIMEOUT = int(os.environ.get("RBTLOG_BUILD_TIMEOUT") or 20)
PODMAN_USE_TASKSET = os.environ.get("RBTLOG_PODMAN_USE_TASKSET") not in ("0", "no", "false")


class Error(Exception):
    """Base class for errors."""


@dataclass(frozen=True)
class CachedTool:
    """Cached tool."""
    name: str
    file_name: str
    version: str
    url: str
    sha256: str

    def for_json(self) -> Dict[str, Any]:
        return dict(name=self.name, file_name=self.file_name,
                    version=self.version, url=self.url, sha256=self.sha256)


@dataclass(frozen=True)
class CachedToolWithContainerCacheDir:
    """Cached tool with container cache dir information."""
    container_cache_dir: str
    cached_tool: CachedTool


@dataclass(frozen=True)
class Download:
    """Download."""
    version: str
    url: str
    sha256: str

    def for_json(self) -> Dict[str, Any]:
        return dict(version=self.version, url=self.url, sha256=self.sha256)


@dataclass(frozen=True)
class Provisioning:
    """Provisioning data."""
    android_home: str
    build_tools: Optional[str]
    cached_tools: list[CachedTool]
    cmake: Optional[str]
    cmdline_tools: Download
    extra_packages: Tuple[str, ...]
    image: str
    jdk: str
    ndk: Optional[str]
    platform: Optional[str]
    platform_tools: Optional[str]
    recurse_git_submodules: Optional[bool]
    tools: Optional[str]
    verify_git: bool
    verify_gradle_wrapper: bool
    windows_like: bool

    def for_json(self) -> Dict[str, Any]:
        return dict(
            android_home=self.android_home, build_tools=self.build_tools,
            cached_tools=[cached_tool.for_json() for cached_tool in self.cached_tools],
            cmake=self.cmake, cmdline_tools=self.cmdline_tools.for_json(), extra_packages=self.extra_packages,
            image=self.image, jdk=self.jdk, ndk=self.ndk, platform=self.platform,
            platform_tools=self.platform_tools, recurse_git_submodules=self.recurse_git_submodules,
            tools=self.tools, verify_git=self.verify_git, verify_gradle_wrapper=self.verify_gradle_wrapper,
            windows_like=self.windows_like)


@dataclass(frozen=True)
class BuildRecipe:
    """Build recipe."""
    repository: str
    tag: str
    apk_pattern: str
    apk_url: Optional[str]
    build: str
    build_cpus: Optional[int]
    build_dirs: Tuple[str, ...]
    build_home_dir: str
    build_repo_dir: str
    build_timeout: Optional[int]
    build_user: str
    provision_root_timeout: Optional[int]
    provision_timeout: Optional[int]
    provisioning: Provisioning

    def for_json(self) -> Dict[str, Any]:
        return dict(
            repository=self.repository, tag=self.tag, apk_pattern=self.apk_pattern,
            apk_url=self.apk_url, build=self.build, build_cpus=self.build_cpus,
            build_dirs=self.build_dirs, build_home_dir=self.build_home_dir,
            build_repo_dir=self.build_repo_dir, build_timeout=self.build_timeout,
            build_user=self.build_user, provision_root_timeout=self.provision_root_timeout,
            provision_timeout=self.provision_timeout, provisioning=self.provisioning.for_json())


@dataclass(frozen=True)
class AppRecipe:
    """App recipe."""
    repository: str
    updates: str
    versions: Tuple[BuildRecipe, ...]


@dataclass(frozen=True)
class TmpApks:
    signed_apk: str
    unsigned_apk: str
    copied_apk: str
    output_apk: str


def apply_modules(recipe_section: Dict[str, Any], modules: Dict[str, str], host_cache_dir: Optional[str] = None, verbose: bool = False) -> Dict[str, Any]:
    # Retrieve list of known modules
    with open("modules.json", encoding="utf-8") as fh:
        modules_config = json.load(fh)        # type: ignore[no-any-return]
        validate_yaml(modules_config, "modules")

    keep_cache = host_cache_dir != None
    if not host_cache_dir:
        host_cache_dir = tempfile.TemporaryDirectory().name

    modules_dir = os.path.join(host_cache_dir, "modules")
    os.makedirs(modules_dir, 0o755, exist_ok=True)

    for module in modules:
        if module['name'] not in modules_config:
            raise Error(f"Could not find module {module['name']} in modules.json")

        module_dir = os.path.join(modules_dir, module['name'])
        has_correct_upstream = False
        if os.path.isdir(module_dir):
            try:
                module_repository_url = run_command("git", "config", "remote.origin.url", cwd=module_dir).strip()
                has_correct_upstream = (module_repository_url == modules_config[module['name']]['repository'])
            except subprocess.CalledProcessError:
                # Failure is also treated as not the correct upstream
                pass

            if verbose and not has_correct_upstream:
                print(f"Cache directory for {module} is not correct, recreating...")
                shutil.rmtree(module_dir)

        if not has_correct_upstream:
            run_command("git", "clone", "-b", modules_config[module['name']]["branch"], modules_config[module['name']]["repository"], module_dir, verbose=verbose)
        elif verbose:
            print(f"Using module {module} from cache", file=sys.stderr)

        try:
            run_command("git", "-C", module_dir, "fetch", verbose=verbose)
        except subprocess.CalledProcessError as e:
            if verbose:
                print(f"Failed to update module {module['name']}, using older cache, this may cause failures if the desired commit is not in this cache: {e}", file=sys.stderr)

        try:
            run_command("git", "-C", module_dir, "checkout", module['commit'], verbose=verbose)
        except subprocess.CalledProcessError as e:
            raise Error(f"Could not switch to commit {module['commit']} for module {module['name']}: {e}")

        module_path = os.path.join(module_dir, "modules", module['name'], f"{module['version']}.yml")
        if not os.path.isfile(module_path):
            raise Error(f"Could not find version {module['version']} of module {module['name']} in repository defined in modules.json in commit {module['commit']}")
        if verbose:
            print(f"Applying module {module}", file=sys.stderr)
        with open(module_path, encoding="utf-8") as fh:
            yaml = YAML(typ="safe")
            data = yaml.load(fh)
            try:
                module_rbtlog_version = data["version"]
            except Exception:
                raise Error(f"Version {module['version']} of module {module['name']} is not compatible with this version of rbtlog or malformed: Could not read version field.")
            if module_rbtlog_version != 1:
                raise Error(f"Version {module['version']} of module {module['name']} is not compatible with this verison of rbtlog: Supported version is 1, got version {module_rbtlog_version}")
            validate_yaml(data, "module")
            for module_step in data["steps"]:
                def apply_module_part(recipe_section: Dict[str, Any], path: list[str], action: str, value: Any) -> dict[str, Any]:
                    part = path.pop(0)
                    if len(path) > 0:
                        recipe_section[part] = apply_module_part(recipe_section[part], path, action, value)
                        return recipe_section

                    # Assume the type is valid if the section is unfilled
                    # We will validate the whole recipe again after
                    if part not in recipe_section:
                        recipe_section[part] = type(value)()

                    match action:
                        case "replace":
                            recipe_section[part] = value
                        case "prepend":
                            recipe_section[part] = value + recipe_section[part]
                        case "append":
                            recipe_section[part] = recipe_section[part] + value
                        case _:
                            raise Error(f"Unknown action in version {module['version']} of module {module['name']}: {action}")

                    return recipe_section

                recipe_section = apply_module_part(recipe_section, module_step["path"].split("."), module_step["action"], module_step["value"])

    if verbose:
        print("Finished applying modules", file=sys.stderr)

    if not keep_cache:
        shutil.rmtree(host_cache_dir)

    return recipe_section


def parse_yaml(recipe_file: str, *, host_cache_dir: Optional[str] = None, verbose: bool = False) -> AppRecipe:
    r"""
    Parse recipe YAML.

    >>> data = parse_yaml("recipes/me.hackerchick.catima.yml")
    >>> data.repository
    'https://github.com/CatimaLoyalty/Android.git'
    >>> data.updates
    'releases'
    >>> data.versions[0]
    BuildRecipe(repository='https://github.com/CatimaLoyalty/Android.git', tag='v2.27.0', apk_pattern='app-release\\.apk', apk_url='https://github.com/CatimaLoyalty/Android/releases/download/v2.27.0/app-release.apk', build='./gradlew assembleRelease\nmv app/build/outputs/apk/release/app-release-unsigned.apk /outputs/unsigned.apk\n', build_cpus=None, build_dirs=(), build_home_dir='/build', build_repo_dir='/build/repo', build_timeout=None, build_user='build', provisioning=Provisioning(android_home='/opt/sdk', build_tools=None, cmake=None, cmdline_tools=Download(version='12.0', url='https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip', sha256='2d2d50857e4eb553af5a6dc3ad507a17adf43d115264b1afc116f95c92e5e258'), extra_packages=(), image='debian:bookworm-slim', jdk='openjdk-17-jdk-headless', ndk=None, platform=None, platform_tools=None, recurse_git_submodules=None, tools=None, verify_git=True, verify_gradle_wrapper=True, windows_like=False))

    """
    with open(recipe_file, encoding="utf-8") as fh:
        yaml = YAML(typ="safe")
        data = yaml.load(fh)
        validate_yaml(data, "recipe")
        versions = []   # FIXME pylint: disable=W0101
        tag_pattern = data["updates"][5:] if data["updates"].startswith("tags:") else None
        for vsn in data["versions"]:
            tag = vsn["tag"]
            for apk in vsn["apks"]:
                # Parse modues and insert result into build recipe
                modules = apk.get("modules", [])
                if modules:
                    apk = apply_modules(apk, modules, host_cache_dir=host_cache_dir, verbose=verbose)
                    # Revalidate after applying modules
                    validation_data = copy.deepcopy(data)
                    validation_data["versions"][0]["apks"][0] = apk
                    validate_yaml(validation_data, "recipe")

                # Parse recipe
                apk_url = url_with_replacements(apk["apk_url"], tag, tag_pattern)
                prov = apk["provisioning"]

                versions.append(BuildRecipe(
                    repository=data["repository"],
                    tag=tag,
                    apk_pattern=apk["apk_pattern"],
                    apk_url=apk_url,
                    build="".join(line + "\n" for line in apk["build"]),
                    build_cpus=apk.get("build_cpus"),
                    build_dirs=tuple(apk.get("build_dirs", [])),
                    build_home_dir=apk["build_home_dir"],
                    build_repo_dir=apk["build_repo_dir"],
                    build_timeout=apk.get("build_timeout"),
                    build_user=apk["build_user"],
                    provision_root_timeout=apk.get("provision_root_timeout"),
                    provision_timeout=apk.get("provision_timeout"),
                    provisioning=Provisioning(
                        android_home=prov["android_home"],
                        build_tools=prov["build_tools"],
                        cached_tools=[
                            CachedTool(
                                name=cached_tool["name"],
                                file_name=cached_tool["file_name"],
                                version=cached_tool["version"],
                                url=cached_tool["url"],
                                sha256=cached_tool["sha256"]
                            )
                            for cached_tool in prov.get("cached_tools", [])
                        ],
                        cmake=prov["cmake"],
                        cmdline_tools=Download(
                            version=prov["cmdline_tools"]["version"],
                            url=prov["cmdline_tools"]["url"],
                            sha256=prov["cmdline_tools"]["sha256"],
                        ),
                        extra_packages=tuple(prov["extra_packages"]),
                        image=prov["image"],
                        jdk=prov["jdk"],
                        ndk=prov["ndk"],
                        platform=prov["platform"],
                        platform_tools=prov["platform_tools"],
                        recurse_git_submodules=prov.get("recurse_git_submodules", True),
                        tools=prov["tools"],
                        verify_git=prov.get("verify_git", True),
                        verify_gradle_wrapper=prov["verify_gradle_wrapper"],
                        windows_like=prov.get("windows_like", False)),
                ))
        return AppRecipe(repository=data["repository"], updates=data["updates"],
                         versions=tuple(versions))


def validate_yaml(data: Dict[str, Any], schema: str) -> None:
    r"""
    Validate YAML or JSON.

    >>> try:
    ...     validate_yaml({}, "recipe")
    ... except jsonschema.exceptions.ValidationError as e:
    ...     e.message
    "'repository' is a required property"
    >>> try:
    ...     validate_yaml({"repository": "oops", "updates": "releases", "versions": []}, "recipe")
    ... except jsonschema.exceptions.ValidationError as e:
    ...     e.message
    "'oops' does not match '^https://.*\\\\.git$'"

    """
    jsonschema.validate(instance=data, schema=_schema(schema))


@functools.lru_cache(maxsize=None)
def _schema(name: str) -> Dict[str, Any]:
    with open(os.path.join("schemas", f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)            # type: ignore[no-any-return]


def url_with_replacements(apk_url: str, tag: str, tag_pattern: Optional[str]) -> str:
    """URL with $$TAG$$ $$TAG:1$$ etc. replaced."""
    url = apk_url.replace("$$TAG$$", tag)
    if tag_pattern and (m := re.fullmatch(tag_pattern, tag)):
        for i, group in enumerate(m.groups("")):
            url = url.replace(f"$$TAG:{i + 1}$$", group)
    return url


# FIXME
def build_with_backend(backend: BuildBackend, appid: str, recipe: BuildRecipe, *,
                       commit: Optional[str] = None, apk_url: Optional[str] = None,
                       container_cache_dir: Optional[str] = None, keep_apks: Optional[str] = None, verbose: bool = False) -> Dict[Any, Any]:
    """Build recipe with backend (e.g. podman/docker)."""
    cpu_count = os.cpu_count()
    if cpu_count and recipe.build_cpus and recipe.build_cpus > cpu_count:
        print(f"Warning: build_cpus={recipe.build_cpus} > cpu_count={cpu_count}", file=sys.stderr)
    if commit:
        recipe = dataclasses.replace(recipe, tag=NOTAG)
    if apk_url:
        recipe = dataclasses.replace(recipe, apk_url=None if apk_url == NOAPK else apk_url)
    result: Dict[str, Any] = dict(
        appid=appid, version_code=None, version_name=None, tag=recipe.tag, commit=commit,
        recipe=recipe.for_json(), timestamp=int(time.time()), cpu_count=cpu_count,
        reproducible=None, error=None, build_log="", upstream_signed_apk_sha256=None,
        built_unsigned_apk_sha256=None, signature_copied_apk_sha256=None, maintainer_notes=None)
    try:
        if not commit:
            commit = result["commit"] = tag_to_commit(recipe.repository, recipe.tag)
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs, scripts, cached_tools_with_container_cache_dir = prepare_tmpdir(recipe, tmpdir, container_cache_dir, verbose=verbose)
            if recipe.apk_url:
                signed_sha, vercode, vername = download_apk(
                    recipe.apk_url, appid, tmpdir, allow_local=bool(apk_url), verbose=verbose)
                result.update(version_code=vercode, version_name=vername,
                              upstream_signed_apk_sha256=signed_sha)
            if backend in (BuildBackend.PODMAN, BuildBackend.DOCKER):
                pull_cmd = (backend.name.lower(), "pull", "--", recipe.provisioning.image)
                run_cmd = podman_docker_cmd(recipe, backend, cached_tools_with_container_cache_dir, commit, outputs=outputs, scripts=scripts, verbose=verbose)
                commands = [pull_cmd, run_cmd]
            else:
                raise NotImplementedError(f"Unknown backend: {backend}")
            try:
                for cmd in commands:
                    result["build_log"] += f"RUN COMMAND {' '.join(cmd)}\n"
                    result["build_log"] += run_command(*cmd, verbose=verbose)
            except subprocess.CalledProcessError as e:
                result["build_log"] += e.stdout.decode()
                raise
            finally:
                if verbose:
                    print(f"--- BEGIN BUILD LOG ---\n{result['build_log']}\n"
                          "--- END BUILD LOG ---", file=sys.stderr)
            rev = commit if recipe.tag == NOTAG else recipe.tag
            if not recipe.apk_url:
                unsigned_sha = keep_built_apk_only(tmpdir, appid=appid, rev=rev,
                                                   keep_apks=keep_apks, verbose=verbose)
                result.update(built_unsigned_apk_sha256=unsigned_sha)
            else:
                unsigned_sha, copied_sha, error = compare_apks(
                    tmpdir, appid=appid, rev=rev, signed_sha=signed_sha,
                    keep_apks=keep_apks, verbose=verbose)
                reproducible = signed_sha == copied_sha
                if verbose:
                    print(f"Reproducible: {reproducible}", file=sys.stderr)
                result.update(
                    reproducible=reproducible,      # FIXME
                    built_unsigned_apk_sha256=unsigned_sha,
                    signature_copied_apk_sha256=copied_sha,
                    error=error,
                )
            return result
    except subprocess.CalledProcessError as e:
        error = f"subprocess failed: {e}"
    except FileNotFoundError as e:
        error = f"command or file not found: {e}"
    except requests.RequestException as e:
        error = f"http error: {e}"
    except binres.Error as e:
        error = f"axml error: {e}"
    except Error as e:
        error = str(e)
    if verbose:
        print(f"Error: {error}", file=sys.stderr)
    return dict(result, error=error)


def prepare_cache(provisioning: Provisioning, tmpdir: str, container_cache_dir: Optional[str] = None, verbose: bool = False) -> dict[str, CachedToolWithContainerCacheDir]:
    r"""
    Prepare cache directories for commonly reused components.

    If container_cache_dir is None, this cache has the same lifecycle as tmpdir. However, if container_cache_dir is defined, data will be persisted in container_cache_dir.

    """
    # General cache logic
    cached_tools = provisioning.cached_tools
    cached_tools.append(
        CachedTool(
            name="cmdline_tools",
            file_name="tools.zip",
            version=provisioning.cmdline_tools.version,
            url=provisioning.cmdline_tools.url,
            sha256=provisioning.cmdline_tools.sha256
        )
    )
    keep_cache = container_cache_dir != None
    if not container_cache_dir:
        container_cache_dir = tempfile.TemporaryDirectory().name
    cached_tools_with_container_cache_dir = {}
    for cached_tool in cached_tools:
        if cached_tool.name.lower() in cached_tools_with_container_cache_dir.keys() or cached_tool.name.lower() == "gradle_wrapper_verify":
            raise Error(f"Cached tool name {cached_tool.name} is not unique or reserved. Cached tool names must be unique and not reserved.")
        container_cache_dir_tool = os.path.join(container_cache_dir, cached_tool.name)
        container_cache_dir_tool_version = os.path.join(container_cache_dir_tool, cached_tool.version)
        if not os.path.exists(container_cache_dir_tool_version):
            os.makedirs(container_cache_dir_tool_version, 0o755)    # allow reading from within container
        container_cache_dir_tool_version_file = os.path.join(container_cache_dir_tool_version, cached_tool.file_name)
        cache_exists = False
        if os.path.exists(container_cache_dir_tool_version_file):
            sha256 = sha256_file(container_cache_dir_tool_version_file)
            if sha256 == cached_tool.sha256:
                cache_exists = True
                if verbose:
                    print(f"Found valid local cache for version {cached_tool.version!r} of {cached_tool.name!r}", file=sys.stderr)
            else:
                if verbose:
                    print(f"Local cache for version {cached_tool.version!r} of {cached_tool.name!r} did not match sha256sum {cached_tool.sha256!r}, deleting...", file=sys.stderr)
                os.remove(container_cache_dir_tool_version_file)
        if not cache_exists:
            if not is_http_url(cached_tool.url):
                raise Error(f"Non-http(s) URL for {cached_tool.name!r}: {cached_tool.url!r}")
            if verbose:
                print(f"Downloading version {cached_tool.version!r} of {cached_tool.name!r} from {cached_tool.url!r}...", file=sys.stderr)
            sha256 = download_file_with_retries(cached_tool.url, container_cache_dir_tool_version_file, retries=5, verbose=verbose)
            if sha256 != cached_tool.sha256:
                raise Error(f"Expected {cached_tool.name!r} to have {cached_tool.sha256!r} as sha256, but got {sha256}")
        # copy to tmpdir to not leak host path during the build
        shutil.copytree(container_cache_dir_tool_version, os.path.join(tmpdir, cached_tool.name))
        cached_tools_with_container_cache_dir[cached_tool.name.lower()] = CachedToolWithContainerCacheDir(container_cache_dir=os.path.join(tmpdir, cached_tool.name), cached_tool=cached_tool)
    # Special case for verify_gradle_wrapper
    if provisioning.verify_gradle_wrapper:
        gradle_wrapper_verify_container_cache_dir = os.path.join(container_cache_dir, "gradle_wrapper_verify")
        if not os.path.exists(gradle_wrapper_verify_container_cache_dir):
            run_command("git", "clone", "https://codeberg.org/IzzyOnDroid/gradle-wrapper-verify/", gradle_wrapper_verify_container_cache_dir, verbose=verbose)
            os.chmod(gradle_wrapper_verify_container_cache_dir, 0o755)    # allow reading from within container
        else:
            try:
                run_command("git", "-C", gradle_wrapper_verify_container_cache_dir, "pull", verbose=verbose)
            except subprocess.CalledProcessError as e:
                if verbose:
                    print(f"Failed to update gradle-wrapper-verify, using older cache: {e}", file=sys.stderr)
        # copy gradle-wrapper-verify script to tmpdir to not leak host path during the build
        shutil.copytree(gradle_wrapper_verify_container_cache_dir, os.path.join(tmpdir, "gradle_wrapper_verify"))
        cached_tools_with_container_cache_dir["gradle_wrapper_verify"] = CachedToolWithContainerCacheDir(
            container_cache_dir=os.path.join(tmpdir, "gradle_wrapper_verify"),
            cached_tool=CachedTool(
                name="gradle_wrapper_verify",
                file_name="gradle-wrapper-verify",
                version="main",
                url="https://codeberg.org/IzzyOnDroid/gradle-wrapper-verify/",
                sha256=sha256_file(os.path.join(tmpdir, "gradle_wrapper_verify", "gradle-wrapper-verify"))
            )
        )
    if not keep_cache:
        shutil.rmtree(container_cache_dir)
    return cached_tools_with_container_cache_dir


def prepare_tmpdir(recipe: BuildRecipe, tmpdir: str, container_cache_dir: Optional[str] = None, verbose: bool = False) -> Tuple[str, str, dict[str, CachedToolWithContainerCacheDir]]:
    """Prepare directories and scripts in tmpdir."""
    outputs = os.path.join(tmpdir, "outputs")
    scripts = os.path.join(tmpdir, "scripts")
    cache = os.path.join(tmpdir, "cache")
    provision_root_sh = os.path.join(scripts, "provision-root.sh")
    provision_sh = os.path.join(scripts, "provision.sh")
    build_sh = os.path.join(scripts, "build.sh")
    os.chmod(tmpdir, 0o700)     # private
    os.mkdir(outputs)
    os.mkdir(scripts)
    os.mkdir(cache)
    os.chmod(outputs, 0o777)    # allow writing from within container
    os.chmod(scripts, 0o755)    # allow reading from within container
    os.chmod(cache, 0o755)      # allow reading from within container
    shutil.copyfile(os.path.join("scripts", "provision-root.sh"), provision_root_sh)
    shutil.copyfile(os.path.join("scripts", "provision.sh"), provision_sh)
    cached_tools_with_container_cache_dir = prepare_cache(recipe.provisioning, cache, container_cache_dir, verbose=verbose)
    os.chmod(provision_root_sh, 0o755)
    os.chmod(provision_sh, 0o755)
    with open(build_sh, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/bash\nset -xeuo pipefail\n")
        fh.write('export PATH="${PATH}:${ANDROID_HOME}/cmdline-tools/'
                 '${PROVISIONING_CACHE_CMDLINE_TOOLS_VERSION}/bin"\n')
        fh.write(recipe.build + "\n")
    os.chmod(build_sh, 0o755)
    return outputs, scripts, cached_tools_with_container_cache_dir


def podman_docker_cmd(recipe: BuildRecipe, backend: BuildBackend, cached_tools_with_container_cache_dir: dict[str, CachedToolWithContainerCacheDir], commit: str, *, outputs: str, scripts: str, verbose: bool = False) -> Tuple[str, ...]:
    """Create podman/docker run command line from recipe."""
    if not recipe.build_cpus:
        taskset: List[str] = []
        cpus: List[str] = []
    elif backend == BuildBackend.PODMAN and PODMAN_USE_TASKSET:
        taskset, cpus = ["taskset", "--cpu-list", f"0-{recipe.build_cpus - 1}"], []
    else:
        taskset, cpus = [], ["--cpus", f"{recipe.build_cpus}"]
    build_timeout = int(recipe.build_timeout or BUILD_TIMEOUT)
    provision_root_timeout = int(recipe.provision_root_timeout or PROVISION_ROOT_TIMEOUT)
    provision_timeout = int(recipe.provision_timeout or PROVISION_TIMEOUT)
    env, benv = [], build_env(recipe, commit)
    for k, v in benv.items():
        env.extend(["--env", f"{k}={v}"])
    cmd = [
        f"timeout {provision_root_timeout}m /scripts/provision-root.sh",
        f"cd {benv['BUILD_HOME_DIR']}",
        f"timeout {provision_timeout}m su {benv['BUILD_USER']} /scripts/provision.sh",
        f"cd {benv['BUILD_REPO_DIR']}",
        f"timeout {build_timeout}m su {benv['BUILD_USER']} /scripts/build.sh",
    ]
    container_cache_dirs = []
    container_cache_prefix = "/rbtlog_cache/"
    for cached_tool_name, cached_tool_info in cached_tools_with_container_cache_dir.items():
        # Replace all potentially misinterpretable characters with underscores
        # and uppercase the env var for consistency
        env_var_safe_cached_tool_name = re.sub(r'[^a-zA-Z0-9]', '_', cached_tool_name.upper())
        cache_host_directory = cached_tool_info.container_cache_dir
        cache_container_directory = os.path.join(container_cache_prefix, cached_tool_name)
        container_cache_dirs.extend(["--volume", f"{cache_host_directory}:{cache_container_directory}:ro"])
        env.extend(["--env", f"PROVISIONING_CACHE_{env_var_safe_cached_tool_name}_PATH={os.path.join(cache_container_directory, cached_tool_info.cached_tool.file_name)}"])
        env.extend(["--env", f"PROVISIONING_CACHE_{env_var_safe_cached_tool_name}_VERSION={cached_tool_info.cached_tool.version}"])
        env.extend(["--env", f"PROVISIONING_CACHE_{env_var_safe_cached_tool_name}_SHA256={cached_tool_info.cached_tool.sha256}"])
    return (
        *taskset, backend.name.lower(), "run", "--rm",
        "--volume", f"{outputs}:/outputs",
        "--volume", f"{scripts}:/scripts:ro",
        *container_cache_dirs,
        *env, *cpus, "--", recipe.provisioning.image,
        "bash", "-c", " && ".join(cmd))


def build_env(recipe: BuildRecipe, commit: str) -> Dict[str, str]:
    """Create build env from recipe."""
    recurse_git_submodules = "yes" if recipe.provisioning.recurse_git_submodules else "no"
    verify_git = "yes" if recipe.provisioning.verify_git else "no"
    verify_gradle_wrapper = "yes" if recipe.provisioning.verify_gradle_wrapper else "no"
    windows_like = "yes" if recipe.provisioning.windows_like else "no"
    return dict(
        ANDROID_HOME=recipe.provisioning.android_home,
        APP_REPOSITORY=recipe.repository,
        APP_TAG=recipe.tag,
        APP_COMMIT=commit,
        BUILD_USER=recipe.build_user,
        BUILD_HOME_DIR=recipe.build_home_dir,
        BUILD_REPO_DIR=recipe.build_repo_dir,
        BUILD_DIRS=":".join(recipe.build_dirs),
        PROVISIONING_BUILD_TOOLS=recipe.provisioning.build_tools or "",
        PROVISIONING_CMAKE=recipe.provisioning.cmake or "",
        PROVISIONING_EXTRA_PACKAGES=",".join(recipe.provisioning.extra_packages),
        PROVISIONING_JDK=recipe.provisioning.jdk,
        PROVISIONING_NDK=recipe.provisioning.ndk or "",
        PROVISIONING_PLATFORM=recipe.provisioning.platform or "",
        PROVISIONING_PLATFORM_TOOLS=recipe.provisioning.platform_tools or "",
        RECURSE_GIT_SUBMODULES=recurse_git_submodules,
        PROVISIONING_TOOLS=recipe.provisioning.tools or "",
        VERIFY_GIT=verify_git,
        VERIFY_GRADLE_WRAPPER=verify_gradle_wrapper,
        WINDOWS_LIKE=windows_like)


# FIXME: configure retries
def download_apk(apk_url: str, appid: str, tmpdir: str, *,
                 allow_local: bool = False, verbose: bool = False) -> Tuple[str, int, str]:
    """Download APK and get versionCode and versionName."""
    signed_apk = os.path.join(tmpdir, "upstream.apk")
    if verbose:
        print(f"Downloading {apk_url!r}...", file=sys.stderr)
    if is_http_url(apk_url):
        sha256 = download_file_with_retries(apk_url, signed_apk, retries=5, verbose=verbose)
    elif allow_local:
        shutil.copyfile(apk_url, signed_apk)
        sha256 = sha256_file(signed_apk)
    else:
        raise Error(f"Non-http(s) URL: {apk_url!r}")
    appid_from_apk, vercode, vername = apk_version_info(signed_apk)
    if appid != appid_from_apk:
        raise Error(f"APK appid mismatch: expected {appid}, got {appid_from_apk}")
    return sha256, vercode, vername


def is_http_url(url: str) -> bool:
    """Does the URL start with http(s)://?"""
    return url.startswith("https://") or url.startswith("http://")


def apk_version_info(apkfile: str) -> Tuple[str, int, str]:
    """Get APK appid, versionCode, versionName."""
    return binres.quick_get_idver(apkfile)


# FIXME: diff on fail?
def compare_apks(tmpdir: str, *, appid: str, rev: str, signed_sha: str,
                 keep_apks: Optional[str] = None, verbose: bool = False) \
        -> Tuple[str, Optional[str], Optional[str]]:
    """Compare upstream APK to built APK (after some checks); optionally save them."""
    ta = tmp_apks(tmpdir)
    unsigned_sha = copy_output_apk(ta.output_apk, ta.unsigned_apk)
    if keep_apks:
        keep_apk(appid, rev, keep_apks, ta.signed_apk, signed_sha, verbose=verbose)
        keep_apk(appid, rev, keep_apks, ta.unsigned_apk, unsigned_sha, verbose=verbose)
    try:
        apksigcopier.do_copy(ta.signed_apk, ta.unsigned_apk, ta.copied_apk, v1_only=None)
        copied_sha = sha256_file(ta.copied_apk)
        error = None
    except (apksigcopier.APKSigCopierError, zipfile.BadZipFile) as e:
        copied_sha = None
        error = f"signature copying failed: {e}"
    return unsigned_sha, copied_sha, error


def keep_built_apk_only(tmpdir: str, *, appid: str, rev: str, keep_apks: Optional[str],
                        verbose: bool = False) -> str:
    """Keep built APK (no upstream APK)."""
    ta = tmp_apks(tmpdir)
    unsigned_sha = copy_output_apk(ta.output_apk, ta.unsigned_apk)
    if keep_apks:
        keep_apk(appid, rev, keep_apks, ta.unsigned_apk, unsigned_sha, verbose=verbose)
    return unsigned_sha


def copy_output_apk(output_apk: str, unsigned_apk: str) -> str:
    """Check & copy output APK and return sha256."""
    if not os.path.isfile(output_apk) or os.path.islink(output_apk):
        raise Error("Unsigned output APK is not a regular file")
    shutil.copyfile(output_apk, unsigned_apk)   # copy w/o permission bits!
    return sha256_file(unsigned_apk)


def tmp_apks(tmpdir: str) -> TmpApks:
    """Temporary APKs."""
    return TmpApks(
        signed_apk=os.path.join(tmpdir, "upstream.apk"),
        unsigned_apk=os.path.join(tmpdir, "unsigned.apk"),
        copied_apk=os.path.join(tmpdir, "copied.apk"),
        output_apk=os.path.join(tmpdir, "outputs", "unsigned.apk"))


def keep_apk(appid: str, rev: str, output_dir: str, apkfile: str, sha256: str, *,
             verbose: bool = False) -> None:
    """Copy APK."""
    rev = reduce(lambda t, c: t.replace(c, "_"), '"*/:<>?\\|', rev)
    target = f"{sha256}-{appid}-{rev}-{os.path.basename(apkfile)}"
    if verbose:
        print(f"Keeping {target!r}...", file=sys.stderr)
    shutil.copyfile(apkfile, os.path.join(output_dir, target))


# FIXME
def build(backend: str, *specs: str, container_cache_dir: Optional[str] = None,
          host_cache_dir: Optional[str] = None, keep_apks: Optional[str] = None,
          local: bool = False, verbose: bool = False) -> int:
    """Parse YAML & build apps."""
    bb = BuildBackend.__members__[backend.upper()]
    errors = 0
    outputs = []
    for spec in specs:
        if verbose:
            print(f"Building {spec!r}...", file=sys.stderr)
        commit: Optional[str]
        apk_url: Optional[str]
        if local and spec.count(":") > 1:
            appid, tag, commit, apk_url = spec.split(":", 3)
            commit, apk_url = commit or None, apk_url or None
        else:
            appid, tag = spec.split(":", 1)
            commit = apk_url = None
        try:
            recipe = parse_yaml(os.path.join("recipes", f"{appid}.yml"), host_cache_dir=host_cache_dir, verbose=verbose)
        except jsonschema.exceptions.ValidationError as e:
            errors += 1
            print(f"Error building {appid!r}: recipe validation error:\n  {e}", file=sys.stderr)
            continue
        build_recipes = [br for br in recipe.versions if br.tag == tag]
        if build_recipes:
            for br in build_recipes:
                out = build_with_backend(bb, appid, br, commit=commit, apk_url=apk_url,
                                         container_cache_dir=container_cache_dir, keep_apks=keep_apks, verbose=verbose)
                outputs.append(out)
                if not out["upstream_signed_apk_sha256"] and apk_url != NOAPK:
                    errors += 1
                    if not verbose:     # already printed otherwise
                        print(f"Error building {spec!r}: {out['error']}", file=sys.stderr)
        else:
            errors += 1
            print(f"Error building {appid!r}: tag not found: {tag!r}", file=sys.stderr)
    json.dump(outputs, sys.stdout, indent=2)
    print()
    return errors


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
    if verbose:
        print(f"Running {' '.join(args)!r}...", file=sys.stderr)
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT).stdout.decode()


# FIXME: configure timeout
def download_file(url: str, output: str) -> str:
    """Download file."""
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(output, "wb") as fh:
            sha = hashlib.sha256()
            for chunk in response.iter_content(chunk_size=4096):
                fh.write(chunk)
                sha.update(chunk)
            return sha.hexdigest()


def download_file_with_retries(url: str, output: str, *, retries: int = 5,
                               verbose: bool = False) -> str:
    """Download file w/ retries."""
    error: Exception = Error("No retries")
    for i in range(retries):
        if i:
            if verbose:
                print("Retrying...")
            time.sleep(1)
        try:
            return download_file(url, output)
        except requests.RequestException as e:
            error = e
    raise error


def sha256_file(filename: str) -> str:
    r"""
    Calculate SHA-256 checksum of file.

    >>> sha256_file("/dev/null")
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

    """
    with open(filename, "rb") as fh:
        sha = hashlib.sha256()
        while chunk := fh.read(4096):
            sha.update(chunk)
        return sha.hexdigest()


def tag_to_commit(repository: str, tag: str) -> str:
    r"""
    Get commit hash for tag.

    >>> tag_to_commit("https://github.com/CatimaLoyalty/Android.git", "v2.27.0")
    '84c343e41f4a09ee3fe6ee0924a3446ae325c4b7'
    >>> tag_to_commit("https://github.com/threema-ch/threema-android.git", "5.2.3")
    '14388d856b28bdbe1417d0f92fed09567263c36e'

    """
    output = run_command("git", "ls-remote", "--tags", "--", repository)
    refs = {}
    for line in output.splitlines():
        commit, ref = line.split("\t", 1)
        refs[ref] = commit
    tag_ref = f"refs/tags/{tag}"
    peeled_ref = tag_ref + "^{}"
    if peeled_ref in refs:
        return refs[peeled_ref]
    if tag_ref in refs:
        return refs[tag_ref]
    raise Error(f"Tag not found: {tag}")


if __name__ == "__main__":
    backends = [name.lower() for name in BuildBackend.__members__]
    parser = argparse.ArgumentParser(description="build apps from recipes")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--cache-dir", metavar="DIR", help="save provisioning cache in DIR")
    parser.add_argument("--keep-apks", metavar="DIR", help="save APKs in DIR")
    parser.add_argument("--local", action="store_true",
                        help="allow APPID:TAG:[COMMIT]:[APK|none] build SPECs")
    parser.add_argument("backend", choices=backends, help="backend")
    parser.add_argument("specs", metavar="SPEC", nargs="*", help="APPID:TAG to build")
    args = parser.parse_args()
    if args.keep_apks and not os.path.isdir(args.keep_apks):
        print(f"Error: {args.keep_apks!r} is not an existing directory", file=sys.stderr)
        sys.exit(1)
    elif build(
        args.backend,
        *args.specs,
        container_cache_dir=os.path.join(args.cache_dir, "container") if args.cache_dir else None,
        host_cache_dir=os.path.join(args.cache_dir, "host") if args.cache_dir else None,
        keep_apks=args.keep_apks,
        local=args.local,
        verbose=args.verbose
    ) != 0:
        sys.exit(1)

# vim: set tw=80 sw=4 sts=4 et fdm=marker :
