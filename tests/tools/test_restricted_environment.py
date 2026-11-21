"""Tests for the restricted subprocess execution backend."""

import ast
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from tools.environments.restricted import (
    RestrictedEnvironment,
    _find_bash,
    _from_shell_path,
    _to_shell_path,
    resolve_restricted_paths,
)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def _clean(output: str) -> str:
    return _from_shell_path(output.strip())


def test_find_bash_returns_executable():
    bash = _find_bash()
    assert Path(bash).is_file()


def test_execute_uses_a_child_process(workspace):
    env = RestrictedEnvironment(cwd=str(workspace), root=str(workspace), timeout=5)
    proc = env._run_bash("printf '%s' $$", timeout=5)
    out, _ = proc.communicate(timeout=5)
    assert isinstance(proc, subprocess.Popen)
    assert proc.pid != os.getpid()
    assert out.strip().isdigit()


def test_resolve_paths_clamps_cwd_outside_root(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    resolved_root, resolved_cwd = resolve_restricted_paths(root, "../elsewhere")
    assert resolved_root == root.resolve()
    assert resolved_cwd == root.resolve()


def test_resolve_paths_allows_relative_cwd_inside_root(workspace):
    (workspace / "sub").mkdir()
    resolved_root, resolved_cwd = resolve_restricted_paths(workspace, "sub")
    assert resolved_root == workspace.resolve()
    assert resolved_cwd == (workspace / "sub").resolve()


def test_build_run_env_forwards_only_allowlisted_names(monkeypatch, workspace):
    monkeypatch.setenv("HERMES_RESTRICTED_ALLOWED", "yes")
    monkeypatch.setenv("HERMES_RESTRICTED_SECRET", "must-not-leak")
    env = RestrictedEnvironment(
        cwd=str(workspace), timeout=5, allowed_env=["HERMES_RESTRICTED_ALLOWED"])
    run_env = env._build_run_env()
    assert run_env["HERMES_RESTRICTED_ALLOWED"] == "yes"
    assert "HERMES_RESTRICTED_SECRET" not in run_env
    assert "HOME" not in run_env


def test_child_process_does_not_inherit_host_secret(monkeypatch, workspace):
    monkeypatch.setenv("HERMES_RESTRICTED_ALLOWED", "yes")
    monkeypatch.setenv("HERMES_RESTRICTED_SECRET", "must-not-leak")
    env = RestrictedEnvironment(
        cwd=str(workspace), root=str(workspace), timeout=10,
        allowed_env=["PATH", "HERMES_RESTRICTED_ALLOWED"])
    env._prepare_command = lambda command: (command, None)
    result = env.execute("env", rewrite_compound_background=False)
    assert result["returncode"] == 0, result
    assert "HERMES_RESTRICTED_ALLOWED=yes" in result["output"]
    assert "HERMES_RESTRICTED_SECRET" not in result["output"]


def test_execute_pwd_reports_directory_inside_root(workspace):
    env = RestrictedEnvironment(cwd=str(workspace), root=str(workspace), timeout=10)
    env._prepare_command = lambda command: (command, None)
    result = env.execute("pwd", rewrite_compound_background=False)
    assert result["returncode"] == 0, result
    assert _clean(result["output"]) == str(workspace.resolve())


def test_parent_traversal_in_command_is_rejected(workspace):
    (workspace.parent / "outside.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")
    env = RestrictedEnvironment(cwd=str(workspace), root=str(workspace), timeout=10)
    with pytest.raises(ValueError, match="traversal"):
        env.execute("cat ../outside.txt", rewrite_compound_background=False)


def test_cd_cannot_escape_root(workspace):
    env = RestrictedEnvironment(cwd=str(workspace), root=str(workspace), timeout=10)
    env._prepare_command = lambda command: (command, None)
    outside = shlex.quote(_to_shell_path(workspace.parent))
    result = env.execute(f"cd {outside}; pwd", rewrite_compound_background=False)
    assert "not allowed" in result["output"]
    assert _clean(result["output"].splitlines()[-1]) == str(workspace.resolve())


def test_command_times_out_and_is_killed(workspace):
    env = RestrictedEnvironment(cwd=str(workspace), root=str(workspace), timeout=1)
    env._prepare_command = lambda command: (command, None)
    started = time.monotonic()
    result = env.execute("sleep 10", timeout=1, rewrite_compound_background=False)
    elapsed = time.monotonic() - started
    assert result["returncode"] == 124, result
    assert result.get("hermes_timed_out") is True
    assert elapsed < 6, elapsed


def test_backend_is_registered_in_dispatch_tables():
    """Static check: importing terminal_tool_backends pulls in every optional SDK,
    so verify the registration tables from the source instead."""
    source = (Path(__file__).resolve().parents[2] / "tools" / "terminal_tool_backends.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    assignments = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assignments.update({
        node.target.id: node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value
    })
    builders = {key.value: value for key, value in zip(
        assignments["_ENV_BUILDERS"].keys, assignments["_ENV_BUILDERS"].values)
        if isinstance(key, ast.Constant)}
    specs = {key.value: value for key, value in zip(
        assignments["_BACKEND_SPECS"].keys, assignments["_BACKEND_SPECS"].values)
        if isinstance(key, ast.Constant)}
    assert builders["restricted"].id == "_build_restricted_env"
    assert "restricted" in specs


def test_create_environment_dispatches_to_restricted(workspace):
    from tools.terminal_tool_backends import _create_environment, _restricted_config_from_config

    config = {
        "restricted_root": str(workspace),
        "restricted_allowed_env": ["PATH", "HOME"],
    }
    env = _create_environment(
        "restricted", image="", cwd=str(workspace), timeout=5,
        restricted_config=_restricted_config_from_config(config))
    assert isinstance(env, RestrictedEnvironment)
    assert env.root == workspace.resolve()
    assert env.allowed_env == frozenset(["PATH", "HOME"])


def test_shell_path_round_trip(workspace):
    assert _from_shell_path(_to_shell_path(workspace)) == str(workspace)
