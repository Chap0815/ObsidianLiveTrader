"""Dependency-boundary regression tests."""

from importlib.metadata import (
    PackageNotFoundError,
    requires as installed_requirements,
    version as installed_version,
)
from pathlib import Path
import re

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGES = {"pytest", "pytest-asyncio"}


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(filename: str) -> set[str]:
    names: set[str] = set()
    for raw in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.add(
            _canonical_name(re.split(r"[<>=!~;\[]", line, maxsplit=1)[0])
        )
    return names


def _locked_versions(filename: str) -> dict[str, Version]:
    active_lines = [
        line.strip()
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    exact_pin = re.compile(
        r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[^\]]+\])?=="
        r"(?P<version>[^\s;]+)(?:\s*;.*)?$"
    )
    matches = [exact_pin.fullmatch(line) for line in active_lines]

    assert all(matches), f"{filename} must contain only exact == pins"
    locked = {
        _canonical_name(match.group("name")): Version(match.group("version"))
        for match in matches
        if match
    }
    assert len(locked) == len(active_lines), f"duplicate pin in {filename}"
    return locked


def test_test_framework_is_dev_only():
    runtime = _requirement_names("requirements.txt") | _requirement_names(
        "requirements.lock"
    )
    dev = _requirement_names("requirements-dev.txt")

    assert TEST_PACKAGES.isdisjoint(runtime)
    assert TEST_PACKAGES <= dev


def test_runtime_lock_is_exact_and_covers_direct_dependencies():
    locked_versions = _locked_versions("requirements.lock")

    direct_requirements = [
        Requirement(line.strip())
        for line in (ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for requirement in direct_requirements:
        locked = locked_versions.get(_canonical_name(requirement.name))
        assert locked is not None, f"missing runtime lock pin for {requirement.name}"
        assert locked in requirement.specifier, (
            f"{requirement.name}=={locked} is outside {requirement.specifier}"
        )


def test_dev_lock_is_exact_disjoint_and_covers_direct_dependencies():
    runtime = set(_locked_versions("requirements.lock"))
    dev_locked = _locked_versions("requirements-dev.lock")
    direct = [
        Requirement(line.strip())
        for line in (ROOT / "requirements-dev.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert runtime.isdisjoint(dev_locked), "dev lock must contain only dev packages"
    for requirement in direct:
        locked = dev_locked.get(_canonical_name(requirement.name))
        assert locked is not None, f"missing dev lock pin for {requirement.name}"
        assert locked in requirement.specifier, (
            f"{requirement.name}=={locked} is outside {requirement.specifier}"
        )


def test_installed_runtime_matches_every_exact_lock_pin():
    """The offline suite must execute against the runtime it claims to validate."""
    mismatches: list[str] = []
    for raw in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        locked = line.split("==", 1)[1].split(";", 1)[0].strip()
        try:
            installed = installed_version(requirement.name)
        except PackageNotFoundError:
            mismatches.append(f"{requirement.name}: missing (locked {locked})")
            continue
        if Version(installed) != Version(locked):
            mismatches.append(
                f"{requirement.name}: installed {installed}, locked {locked}"
            )

    assert not mismatches, "runtime lock drift:\n" + "\n".join(mismatches)


def test_installed_dev_tools_match_every_exact_lock_pin():
    mismatches: list[str] = []
    for name, locked in _locked_versions("requirements-dev.lock").items():
        try:
            installed = Version(installed_version(name))
        except PackageNotFoundError:
            mismatches.append(f"{name}: missing (locked {locked})")
            continue
        if installed != locked:
            mismatches.append(f"{name}: installed {installed}, locked {locked}")

    assert not mismatches, "dev lock drift:\n" + "\n".join(mismatches)


def test_lockfiles_cover_the_active_installed_dependency_closure():
    locked = {
        **_locked_versions("requirements.lock"),
        **_locked_versions("requirements-dev.lock"),
    }
    requested_extras: dict[str, set[str]] = {
        package: set() for package in locked
    }
    problems: set[str] = set()

    extras_changed = True
    while extras_changed:
        extras_changed = False
        for package, package_version in locked.items():
            environments = []
            for extra in {"", *requested_extras[package]}:
                environment = default_environment()
                environment["extra"] = extra
                environments.append(environment)

            for raw in installed_requirements(package) or []:
                requirement = Requirement(raw)
                if requirement.marker is not None and not any(
                    requirement.marker.evaluate(environment)
                    for environment in environments
                ):
                    continue
                dependency = _canonical_name(requirement.name)
                dependency_version = locked.get(dependency)
                if dependency_version is None:
                    problems.add(f"{package}: missing dependency pin for {dependency}")
                    continue
                if dependency_version not in requirement.specifier:
                    problems.add(
                        f"{package}=={package_version}: "
                        f"{dependency}=={dependency_version} "
                        f"is outside {requirement.specifier}"
                    )
                dependency_extras = requested_extras[dependency]
                new_extras = set(requirement.extras) - dependency_extras
                if new_extras:
                    dependency_extras.update(new_extras)
                    extras_changed = True

    assert not problems, "incomplete locked dependency closure:\n" + "\n".join(
        sorted(problems)
    )


def test_ci_installs_exact_lockfiles_without_dependency_resolution():
    workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text(
        encoding="utf-8"
    )
    install = next(
        line.strip()
        for line in workflow.splitlines()
        if " pip install " in line and "requirements.lock" in line
    )

    assert "--no-deps" in install
    assert "-r requirements.lock" in install
    assert "-r requirements-dev.lock" in install
    assert "requirements-dev.txt" not in install


def test_pip_bootstrap_pin_is_consistent_across_install_paths():
    pip_requirement = f"pip=={_locked_versions('requirements-dev.lock')['pip']}"
    launch = (ROOT / "scripts" / "launch.py").read_text(encoding="utf-8")
    launch_pin = re.search(
        r'^PINNED_PIP_REQUIREMENT = "(?P<requirement>pip==[^"]+)"$',
        launch,
        re.MULTILINE,
    )

    assert launch_pin is not None
    assert launch_pin.group("requirement") == pip_requirement
    for source in (
        ROOT / ".github" / "workflows" / "checks.yml",
        ROOT / "README.md",
    ):
        assert f" pip install --require-virtualenv {pip_requirement}" in (
            source.read_text(encoding="utf-8")
        )


def test_dependency_audits_are_strict_and_runtime_audit_does_not_resolve():
    sources = (
        ROOT / ".github" / "workflows" / "checks.yml",
        ROOT / "README.md",
    )
    for source in sources:
        audit_commands = [
            line.strip()
            for line in source.read_text(encoding="utf-8").splitlines()
            if " -m pip_audit " in line
        ]

        runtime_audit = next(
            command for command in audit_commands if "-r requirements.lock" in command
        )
        local_audit = next(
            command for command in audit_commands if "--local" in command
        )

        assert "--strict" in runtime_audit
        assert "--no-deps" in runtime_audit
        assert "--disable-pip" in runtime_audit
        assert "--vulnerability-service osv" in runtime_audit
        assert "--strict" in local_audit
        assert "--vulnerability-service osv" in local_audit
