"""Restricted execution environment — subprocess with a cwd jail and cleaned env.

Commands run through ``bash -c`` in a child process. Unlike :class:`LocalEnvironment`
this backend never inherits the host environment, starts every command inside a
configured root directory, and replaces ``cd`` with a guard that refuses to leave
that root. It is intentionally lightweight (no namespaces/seccomp): enough to
keep ordinary commands and path traversal inside the configured workspace.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Iterable
from pathlib import Path

from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _pipe_stdin

_IS_WINDOWS = platform.system() == "Windows"
_DEFAULT_ALLOWED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
# Never inherit shell startup hooks even when they appear in an allowlist: they
# execute arbitrary files before the command body and would defeat env cleaning.
_ALWAYS_DROP_ENV = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "PROMPT_COMMAND"})
# A parent-directory segment in the command text is almost always an attempt to
# step outside the jail. Deliberately conservative: harmless uses of ".." as a
# literal string are also refused in this lightweight backend.
_PARENT_TRAVERSAL_RE = re.compile(r"(?:^|[^A-Za-z0-9_.-])\.\.(?:[^A-Za-z0-9_.-]|$)")


def _to_shell_path(path: str | os.PathLike) -> str:
    """Native path -> path Git Bash / POSIX bash understands."""
    text = os.fspath(path)
    if _IS_WINDOWS and len(text) >= 3 and text[1] == ":" and text[2] in "\\/":
        return "/" + text[0].lower() + "/" + text[2:].replace("\\", "/").lstrip("/")
    return text.replace("\\", "/") if _IS_WINDOWS else text


def _from_shell_path(path: str) -> str:
    """MSYS ``/e/foo`` -> native ``E:\\foo``; no-op on POSIX/other paths."""
    if _IS_WINDOWS and path.startswith("/") and len(path) >= 3 and path[2] == "/":
        return f"{path[1].upper()}:{path[2:].replace('/', chr(92))}"
    return path


def _is_within(child: str | os.PathLike, root: str | os.PathLike) -> bool:
    """True when *child* is *root* itself or a descendant of it."""
    child_text, root_text = os.fspath(child), os.fspath(root)
    try:
        return os.path.commonpath([child_text, root_text]) == root_text
    except ValueError:  # different drives on Windows
        return False


def _find_bash() -> str:
    """Locate a usable bash, skipping the Windows System32 WSL shim."""
    candidates: list[str] = []
    if _IS_WINDOWS:
        for roots in (os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("ProgramData")):
            if roots:
                candidates.append(os.path.join(roots, "Git", "bin", "bash.exe"))
        # System32/WindowsApps `bash.exe` are WSL launchers, not a usable bash
        # when no distro is installed: hide those PATH entries while searching.
        entries = [p for p in os.environ.get("PATH", "").split(os.pathsep)
                   if p and "system32" not in p.lower() and "windowsapps" not in p.lower()]
        found = shutil.which("bash", path=os.pathsep.join(entries))
        if found:
            candidates.append(found)
    else:
        candidates.extend(("/bin/bash", "/usr/bin/bash"))
        found = shutil.which("bash")
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and not (
                _IS_WINDOWS and ("system32" in candidate.lower() or "windowsapps" in candidate.lower())):
            return candidate
    raise RuntimeError("Restricted backend needs bash; no bash executable was found")


def resolve_restricted_paths(root: str | os.PathLike | None, cwd: str | os.PathLike | None):
    """Resolve ``(root, cwd)``; a relative cwd is relative to *root*.

    A cwd outside *root* is clamped to *root*: ``terminal.cwd`` is a global
    setting that may point at a container-style path (e.g. ``/root``), while
    the restricted backend promises to stay inside its own workspace.
    """
    base = Path(root or cwd or os.getcwd()).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    base_root = base.resolve()
    if not base_root.is_dir():
        raise ValueError(f"Restricted backend root is not a directory: {base_root}")

    if not cwd:
        work = base_root
    else:
        work = Path(cwd).expanduser()
        if not work.is_absolute() and "~" not in str(cwd):
            work = base_root / work
        work = work.resolve()
    if not _is_within(work, base_root):
        work = base_root
    return base_root, work


class RestrictedEnvironment(BaseEnvironment):
    """Run commands in a child process confined to ``root`` with a cleaned env.

    ``allowed_env`` names the only host variables forwarded (plus equivalent
    values explicitly supplied through ``env``). Defaults keep common CLI tools
    usable: PATH, HOME, LANG, LC_ALL and TERM.
    """

    # Commands live on this host, but a confined sandbox must not expose host
    # files to the file tools: see BaseEnvironment.uses_host_filesystem.
    is_local = False
    uses_host_filesystem = False
    # Do not run a login shell or write a shell-state snapshot: either would
    # reintroduce host profile variables that the whitelist is meant to block.
    _prefer_nonlogin = True
    _persist_env = False

    def __init__(
        self,
        cwd: str = "",
        timeout: int = 60,
        root: str | None = None,
        allowed_env: Iterable[str] | None = None,
        env: dict | None = None,
    ):
        self.root, work = resolve_restricted_paths(root, cwd)
        self.allowed_env = frozenset(allowed_env if allowed_env is not None else _DEFAULT_ALLOWED_ENV)
        super().__init__(cwd=str(work), timeout=timeout, env=env, persist_env=False)

    # --- Shell helpers ---
    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        return shlex.quote(_to_shell_path(cwd))

    def _quote_shell_path(self, path: str) -> str:
        return shlex.quote(_to_shell_path(path))

    def _cd_guard_script(self) -> str:
        """Bash preamble: replace ``cd`` with a root-confining function.

        The base wrapper uses ``builtin cd`` for its own marker bookkeeping, so
        it is unaffected; a user command calling ``cd ..`` / ``cd /tmp`` hits
        this function and stays inside the configured root.
        """
        root = shlex.quote(_to_shell_path(self.root))
        return "\n".join([
            f"__hermes_restricted_root={root}",
            "cd() {",
            '  local __h_target="$1" __h_resolved',
            '  [ "$#" -eq 0 ] && __h_target="$HOME"',
            '  [ "$__h_target" = "-" ] && __h_target="${OLDPWD:-$PWD}"',
            '  case "$__h_target" in',
            '    /*) ;;',
            '    *) __h_target="$PWD/$__h_target" ;;',
            '  esac',
            '  __h_resolved=$(builtin cd -- "$__h_target" 2>/dev/null && pwd -P) || {',
            '    echo "restricted: cannot enter $__h_target" >&2',
            '    return 1',
            '  }',
            '  case "$__h_resolved/" in',
            '    "$__hermes_restricted_root"/*) ;;',
            '    *) echo "restricted: cd outside $__hermes_restricted_root is not allowed" >&2; return 1 ;;',
            '  esac',
            '  builtin cd -- "$__h_resolved"',
            '}',
            "readonly -f cd",
        ])

    def execute(self, command: str, *args, **kwargs) -> dict:
        if _PARENT_TRAVERSAL_RE.search(command):
            raise ValueError(
                "restricted backend rejects parent-directory traversal ('..') in commands")
        return super().execute(command, *args, **kwargs)

    def _wrap_command(self, command: str, cwd: str, *, cwd_fallback: bool = False) -> str:
        return self._cd_guard_script() + "\n" + super()._wrap_command(
            command, cwd, cwd_fallback=cwd_fallback)

    def _build_run_env(self) -> dict[str, str]:
        """Build the *entire* child environment from the allowlist only.

        Host values are copied only for whitelisted names; explicit ``env``
        values override them. No ``os.environ`` spread, no inherited shell
        startup hooks, and no parent PATH unless PATH was allowlisted.
        """
        env: dict[str, str] = {}
        for name in sorted(self.allowed_env):
            if name in _ALWAYS_DROP_ENV:
                continue
            if name in os.environ:
                env[name] = os.environ[name]
            if name in self.env and self.env[name] is not None:
                env[name] = str(self.env[name])
        if "HOME" in self.allowed_env and "HOME" not in env:
            env["HOME"] = _to_shell_path(self.root)
        if "TERM" in self.allowed_env and "TERM" not in env:
            env["TERM"] = "dumb"
        return env

    # --- Process lifecycle ---
    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        bash = _find_bash()
        run_env = self._build_run_env()
        kwargs = dict(
            text=True, encoding="utf-8", errors="replace", env=run_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            cwd=str(self.root),
        )
        if not _IS_WINDOWS:
            kwargs["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen([bash, "-c", cmd_string], **kwargs)
        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)
        return proc

    def _signal_tree(self, proc, sig: int) -> None:
        if proc.poll() is not None:
            return
        if _IS_WINDOWS:  # no SIGKILL on Windows; terminate()/kill() are the portable calls
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
            return
        try:
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, sig)
                return
        except OSError:
            pass
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.send_signal(sig)

    def _kill_process(self, proc) -> None:
        try:
            self._signal_tree(proc, signal.SIGTERM)
        except OSError:
            pass

    def _force_kill_process(self, proc) -> None:
        try:
            self._signal_tree(proc, getattr(signal, "SIGKILL", 9))
        except OSError:
            pass

    def _parse_cwd_marker(self, result: dict) -> None:
        super()._parse_cwd_marker(result)
        if result.get("cwd_observed"):
            native = _from_shell_path(result["cwd"])
            candidate = Path(native)
            if _is_within(candidate, self.root) and candidate.is_dir():
                result["cwd"] = str(candidate)
            else:
                result.pop("cwd_observed", None)
                result.pop("cwd", None)

    def cleanup(self) -> None:
        self.stop_all_processes()


__all__ = ["RestrictedEnvironment", "_find_bash", "_is_within", "resolve_restricted_paths"]
