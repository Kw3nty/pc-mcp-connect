#!/usr/bin/env python3
"""
pc-mcp: a small MCP server that gives an AI agent your PC's files and terminal over HTTP.

  python pc_mcp_server.py [--port 8765] [--root C:\\Projects]

Prints the secret MCP path (a random token kept in .pc_mcp_token next to this script). Expose the port with a
tunnel (cloudflared tunnel --url http://localhost:8765) and give the agent:
  https://<tunnel-host>/<token>/mcp
Anyone with that URL controls your PC as your user: keep it secret, stop the server (Ctrl+C) when done.
Every call is printed in this window.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import locale
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

try:
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:  # older SDK: no host checks at all
    TransportSecuritySettings = None

IS_WIN = os.name == "nt"
MAX_OUT = 60_000  # characters of command output returned per stream (the tail is kept)
HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser(description="MCP server: files + terminal of this PC")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 and use a tunnel")
ap.add_argument("--root", default=str(Path.home()), help="default working directory for relative paths")
ap.add_argument("--token", default=None, help="secret path token (default: saved in .pc_mcp_token)")
ap.add_argument("--new-token", action="store_true", help="generate a new token (old URL stops working)")
args = ap.parse_args()

ROOT = Path(args.root).expanduser().resolve()
tok_file = HERE / ".pc_mcp_token"
if args.token:
    TOKEN = args.token
elif tok_file.exists() and not args.new_token:
    TOKEN = tok_file.read_text().strip()
else:
    TOKEN = secrets.token_urlsafe(24)
    tok_file.write_text(TOKEN)
PATH = f"/{TOKEN}/mcp"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def resolve(p: str | None) -> Path:
    q = Path(os.path.expandvars(os.path.expanduser(p or ".")))
    return (q if q.is_absolute() else ROOT / q).resolve()


def decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        enc = "cp866" if IS_WIN else (locale.getpreferredencoding(False) or "latin-1")
        return b.decode(enc, errors="replace")


def tail(s: str, n: int = MAX_OUT) -> str:
    return s if len(s) <= n else f"...[{len(s) - n} chars cut]...\n" + s[-n:]


def shell_argv(command: str, shell: str) -> list[str] | str:
    if shell == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "bash":
        return [shutil.which("bash") or "bash", "-lc", command]
    if shell == "cmd":
        return ["cmd.exe", "/d", "/s", "/c", command]
    return command  # default system shell (cmd.exe on Windows, /bin/sh elsewhere)


def popen(command: str, cwd: Path, shell: str, env: dict | None) -> subprocess.Popen:
    argv = shell_argv(command, shell)
    kw: dict = dict(cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                    shell=isinstance(argv, str), env={**os.environ, **(env or {})})
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def kill_tree(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        p.kill()


security = None
if TransportSecuritySettings is not None:
    # Requests arrive through the tunnel with its host name; the secret path is the protection.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

kw = dict(host=args.host, port=args.port, streamable_http_path=PATH, stateless_http=True)
if security is not None:
    kw["transport_security"] = security
mcp = FastMCP("pc", instructions=(
    f"Files and terminal of the user's PC ({sys.platform}, Python {sys.version.split()[0]}). "
    f"Relative paths and the default working directory are {ROOT}. Use start_process for servers / watchers."
), **kw)


@mcp.tool()
def system_info() -> dict:
    """OS, user, default directory, available tools (python, node, git...)."""
    log("system_info")
    tools = {t: shutil.which(t) for t in ["python", "python3", "py", "node", "npm", "npx", "git", "pwsh", "powershell", "bash"]}
    return {"platform": sys.platform, "os": os.name, "user": os.environ.get("USERNAME") or os.environ.get("USER"),
            "root": str(ROOT), "cwd_default": str(ROOT), "python": sys.version, "tools": {k: v for k, v in tools.items() if v}}


@mcp.tool()
def run_command(command: str, cwd: str | None = None, timeout_s: float = 120, shell: str = "default",
                env: dict[str, str] | None = None) -> dict:
    """
    Run a shell command and wait for it. shell: "default" (cmd.exe on Windows, sh elsewhere), "powershell",
    "bash" or "cmd". Output tails are returned (stdout / stderr). The process tree is killed after timeout_s.
    For long-running things (dev servers, watchers) use start_process.
    """
    wd = resolve(cwd)
    log(f"run [{wd}] {command}")
    t0 = time.time()
    p = popen(command, wd, shell, env)
    try:
        out, err = p.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(p)
        out, err = p.communicate()
        timed_out = True
    res = {"exit_code": p.returncode, "timed_out": timed_out, "seconds": round(time.time() - t0, 2),
           "stdout": tail(decode(out)), "stderr": tail(decode(err))}
    log(f"  -> exit {p.returncode}{' (timeout)' if timed_out else ''} in {res['seconds']}s")
    return res


class Bg:
    def __init__(self, p: subprocess.Popen, command: str):
        self.p, self.command, self.out = p, command, []
        self.lock = threading.Lock()
        for s in (p.stdout, p.stderr):
            threading.Thread(target=self.pump, args=(s,), daemon=True).start()

    def pump(self, s) -> None:
        for line in iter(s.readline, b""):
            with self.lock:
                self.out.append(decode(line))


procs: dict[int, Bg] = {}


@mcp.tool()
def start_process(command: str, cwd: str | None = None, shell: str = "default", env: dict[str, str] | None = None) -> dict:
    """Start a long-running command in the background; returns its id. Read output with read_process."""
    wd = resolve(cwd)
    log(f"start [{wd}] {command}")
    p = popen(command, wd, shell, env)
    procs[p.pid] = Bg(p, command)
    return {"id": p.pid}


@mcp.tool()
def read_process(id: int, from_line: int = 0, wait_s: float = 0) -> dict:
    """Output lines (stdout + stderr) of a background process from line `from_line`; optionally wait first."""
    b = procs.get(id)
    if not b:
        return {"error": f"no process {id}"}
    if wait_s > 0:
        time.sleep(min(wait_s, 120))
    with b.lock:
        lines = b.out[from_line:]
        total = len(b.out)
    return {"running": b.p.poll() is None, "exit_code": b.p.poll(), "next_line": total, "output": tail("".join(lines))}


@mcp.tool()
def stop_process(id: int) -> dict:
    """Stop a background process (and its children)."""
    b = procs.get(id)
    if not b:
        return {"error": f"no process {id}"}
    log(f"stop {id} {b.command}")
    kill_tree(b.p)
    return {"stopped": True, "exit_code": b.p.poll()}


@mcp.tool()
def list_processes() -> list[dict]:
    """Background processes started by this server."""
    return [{"id": k, "command": b.command, "running": b.p.poll() is None, "exit_code": b.p.poll()} for k, b in procs.items()]


@mcp.tool()
def read_file(path: str, start_line: int = 1, max_lines: int = 2000) -> dict:
    """Read a text file (UTF-8, other encodings decoded best-effort): lines start_line.. (1-based)."""
    f = resolve(path)
    log(f"read {f}")
    text = decode(f.read_bytes())
    lines = text.splitlines(keepends=True)
    part = lines[max(0, start_line - 1): max(0, start_line - 1) + max_lines]
    return {"path": str(f), "total_lines": len(lines), "start_line": start_line, "content": "".join(part)}


@mcp.tool()
def write_file(path: str, content: str, append: bool = False) -> dict:
    """Write (or append) a UTF-8 text file; creates missing folders."""
    f = resolve(path)
    log(f"{'append' if append else 'write'} {f} ({len(content)} chars)")
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a" if append else "w", encoding="utf-8", newline="") as h:
        h.write(content)
    return {"path": str(f), "bytes": f.stat().st_size}


@mcp.tool()
def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> dict:
    """Replace exact text in a file (the first match, or all with replace_all). Fails if `old` is not found."""
    f = resolve(path)
    text = f.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        return {"error": "old text not found", "path": str(f)}
    text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    with open(f, "w", encoding="utf-8", newline="") as h:
        h.write(text)
    log(f"edit {f} ({n if replace_all else 1} replacement(s))")
    return {"path": str(f), "replaced": n if replace_all else 1, "matches": n}


@mcp.tool()
def read_file_base64(path: str, max_bytes: int = 8_000_000) -> dict:
    """Read a binary file as base64 (images, zips...)."""
    f = resolve(path)
    log(f"read(b64) {f}")
    data = f.read_bytes()[:max_bytes]
    return {"path": str(f), "size": f.stat().st_size, "base64": base64.b64encode(data).decode()}


@mcp.tool()
def write_file_base64(path: str, base64_data: str) -> dict:
    """Write a binary file from base64; creates missing folders."""
    f = resolve(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(base64.b64decode(base64_data))
    log(f"write(b64) {f} ({f.stat().st_size} bytes)")
    return {"path": str(f), "bytes": f.stat().st_size}


@mcp.tool()
def list_dir(path: str = ".", recursive: bool = False, pattern: str = "*", max_entries: int = 1000) -> dict:
    """List a folder (name, type, size). recursive skips node_modules / .git / venvs; pattern is a glob on names."""
    d = resolve(path)
    log(f"ls {d}{' -R' if recursive else ''}")
    skip = {"node_modules", ".git", "__pycache__", ".venv", "venv"}
    out = []
    it = d.rglob("*") if recursive else d.iterdir()
    for e in it:
        if recursive and any(part in skip for part in e.relative_to(d).parts[:-1]):
            continue
        if not fnmatch.fnmatch(e.name, pattern):
            continue
        try:
            st = e.stat()
            out.append({"path": str(e.relative_to(d)), "dir": e.is_dir(), "size": None if e.is_dir() else st.st_size})
        except OSError:
            continue
        if len(out) >= max_entries:
            break
    return {"dir": str(d), "entries": out, "truncated": len(out) >= max_entries}


@mcp.tool()
def delete_path(path: str) -> dict:
    """Delete a file or a whole folder."""
    f = resolve(path)
    log(f"delete {f}")
    if f.is_dir():
        shutil.rmtree(f)
    else:
        f.unlink()
    return {"deleted": str(f)}


if __name__ == "__main__":
    print("=" * 70)
    print(f" pc-mcp: files + terminal of this PC (default dir {ROOT})")
    print(f" local:   http://{args.host}:{args.port}{PATH}")
    print(f" tunnel:  cloudflared tunnel --url http://localhost:{args.port}")
    print(f" give the agent:  https://<tunnel-host>{PATH}")
    print(" keep the URL secret; Ctrl+C stops the server")
    print("=" * 70, flush=True)
    mcp.run(transport="streamable-http")
