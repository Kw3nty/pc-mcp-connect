"""PC MCP Connect — значок в трее, который даёт постоянную ссылку на MCP-сервер этого компьютера.

Режимы:
  shared      общий сервер-посредник (relay): ничего настраивать не нужно
  own         свой сервер-посредник (Hugging Face Space, VPS…): адрес и секрет в настройках
  ngrok       туннель ngrok (сам идёт через локальный прокси VPN, если он найден)
  cloudflare  быстрый туннель Cloudflare (адрес меняется при каждом запуске)

Запуск: PcMcpConnect.exe (или python pcmcp_connect.py) — открывает окно и значок в трее.
  --minimized             сразу в трей, без окна (так запускается автозапуск)
  --headless [--seconds N]  без окна и трея, для проверок
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "PC MCP Connect"
VERSION = "1.0.0"
FROZEN = bool(getattr(sys, "frozen", False))
HERE = Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", HERE))
_HOME = os.environ.get("PCMCP_HOME")
APPDATA = Path(_HOME) if _HOME else Path(os.environ.get("APPDATA") or Path.home()) / "PcMcpConnect"
LOCAL = Path(_HOME) if _HOME else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PcMcpConnect"
CFG_FILE = APPDATA / "config.json"
LOG_FILE = LOCAL / "app.log"
SERVER_LOG = LOCAL / "server.log"
BIN = LOCAL / "bin"
NL = chr(10)
BS = chr(92)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_MSG = 64 * 1024 * 1024
CHUNK = 512 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
MODES = {
    "shared": "Общий сервер (ничего настраивать не нужно)",
    "own": "Свой сервер",
    "ngrok": "ngrok",
    "cloudflare": "Cloudflare (адрес меняется)",
}
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
NGROK_URL = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
RUN_KEY = BS.join(["Software", "Microsoft", "Windows", "CurrentVersion", "Run"])
RUN_VALUE = "PcMcpConnect"

log = logging.getLogger("pcmcp")


def _bundle_hint() -> None:  # pragma: no cover
    """Не вызывается. Нужна, чтобы PyInstaller уложил MCP-сервер и его зависимости в exe."""
    import pc_mcp_server  # noqa: F401


# ---------------------------------------------------------------- MCP-сервер (дочерний процесс)

class _MaskedStream:
    def __init__(self, f, secret: str):
        self.f, self.secret = f, secret

    def write(self, s):
        return self.f.write(s.replace(self.secret, "***") if self.secret else s)

    def flush(self):
        self.f.flush()

    def isatty(self):
        return False


def serve_main(argv: list[str]) -> None:
    """PcMcpConnect.exe --serve ... — запускает сам MCP-сервер (pc_mcp_server.py)."""
    token = argv[argv.index("--token") + 1] if "--token" in argv else ""
    if sys.stdout is None or sys.stderr is None:
        LOCAL.mkdir(parents=True, exist_ok=True)
        f = _MaskedStream(open(SERVER_LOG, "a", encoding="utf-8", buffering=1), token)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    else:
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:  # noqa: BLE001
                pass
    if os.name == "nt":
        # У приложения нет консоли: без этого каждая команда из Notion мигала бы чёрным окном.
        orig_init = subprocess.Popen.__init__

        def patched_init(self, *a, **kw):
            flags = kw.get("creationflags", 0) or 0
            if not flags & 0x10 and not flags & 0x08:  # CREATE_NEW_CONSOLE / DETACHED_PROCESS
                kw["creationflags"] = flags | NO_WINDOW
            orig_init(self, *a, **kw)

        subprocess.Popen.__init__ = patched_init
    import runpy
    sys.argv = ["pc_mcp_server.py"] + argv
    runpy.run_module("pc_mcp_server", run_name="__main__", alter_sys=True)


# ---------------------------------------------------------------- общие функции

def setup_logging(console: bool) -> None:
    LOCAL.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if console and sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def short_err(e: BaseException) -> str:
    return (str(e) or e.__class__.__name__).replace(NL, " ")[:160]


def channel_of(key: str) -> str:
    """Канал на сервере-посреднике = хеш от секретного ключа (та же функция в relay.py)."""
    digest = hashlib.sha256(("pcmcp-channel:" + key).encode()).digest()
    return base64.b32encode(digest).decode().lower().rstrip("=")[:20]


def mask_link(link: str, token: str) -> str:
    return link.replace(token, "***") if token else link


def load_defaults() -> dict:
    d = {"shared_relay_url": ""}
    for p in (RES / "defaults.json", HERE.parent / "defaults.json", Path(sys.executable).parent / "defaults.json"):
        try:
            d.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return d


class Config(dict):
    def __init__(self) -> None:
        super().__init__()
        self.defaults = load_defaults()
        self.update({"mode": "", "shared_relay_url": "", "own_relay_url": "", "own_relay_secret": "",
                     "ngrok_authtoken": "", "ngrok_domain": "", "ngrok_path": "", "cloudflared_path": "",
                     "proxy": "auto", "port": 8770, "root": str(Path.home()), "token": "", "channel_key": "",
                     "first_run_done": False})
        try:
            self.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось прочитать настройки (%s), использую стандартные", e)
        if self["mode"] not in MODES:
            self["mode"] = "shared" if self.shared_url() else "cloudflare"
        if len(self["token"] or "") < 16:
            self["token"] = secrets.token_urlsafe(24)
        if len(self["channel_key"] or "") < 32:
            self["channel_key"] = secrets.token_urlsafe(32)
        self.save()

    def shared_url(self) -> str:
        return (self.get("shared_relay_url") or self.defaults.get("shared_relay_url") or "").strip().rstrip("/")

    def save(self) -> None:
        APPDATA.mkdir(parents=True, exist_ok=True)
        tmp = CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CFG_FILE)


# ---------------------------------------------------------------- прокси VPN

COMMON_PROXIES = [(10809, "http"), (10808, "socks5"), (7890, "http"), (7891, "socks5"), (2081, "http"),
                  (2080, "socks5"), (1080, "socks5"), (10811, "http"), (10810, "socks5"), (20171, "http"),
                  (20170, "socks5"), (12334, "http"), (12335, "socks5")]
_proxy_cache: tuple[float, list[str]] = (0.0, [])


def _probe(port: int, kind: str) -> bool:
    crlf = bytes((13, 10))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6) as s:
            s.settimeout(3)
            if kind == "socks5":
                s.sendall(bytes((5, 1, 0)))
                return s.recv(2) == bytes((5, 0))
            s.sendall(b"CONNECT example.com:443 HTTP/1.1" + crlf + b"Host: example.com:443" + crlf + crlf)
            first = s.recv(128).split(crlf)[0]
            return first.startswith(b"HTTP/1.") and b" 200" in first
    except OSError:
        return False


def detect_proxies() -> list[str]:
    """Ищет локальный прокси VPN-клиента (Happ, v2rayN, Clash, Hiddify…) на стандартных портах."""
    global _proxy_cache
    if time.time() - _proxy_cache[0] < 120:
        return list(_proxy_cache[1])
    found = []
    for port, kind in COMMON_PROXIES:
        for k in (kind, "socks5" if kind == "http" else "http"):
            if _probe(port, k):
                found.append(f"{k}://127.0.0.1:{port}")
                break
    _proxy_cache = (time.time(), found)
    if found:
        log.info("Найден локальный прокси VPN: %s", ", ".join(found))
    return found


def reset_proxy_cache() -> None:
    global _proxy_cache
    _proxy_cache = (0.0, [])


def has_socks() -> bool:
    try:
        import aiohttp_socks  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------- внешние программы (ngrok, cloudflared)

def http_download(url: str, dest: Path, proxies: list[str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    errors = []
    for proxy in [None] + [p for p in proxies if p.startswith("http")]:
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
            log.info("Скачиваю %s%s", url, f" через {proxy}" if proxy else "")
            req = urllib.request.Request(url, headers={"User-Agent": f"pcmcp-connect/{VERSION}"})
            with opener.open(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.replace(dest)
            return
        except Exception as e:  # noqa: BLE001
            errors.append(f"{proxy or 'напрямую'}: {short_err(e)}")
    raise RuntimeError("не удалось скачать " + url.rsplit("/", 1)[-1] + " (" + "; ".join(errors) + ")")


def ensure_tool(name: str, custom: str, url: str, proxies: list[str]) -> str:
    cands = [Path(custom)] if custom else []
    cands += [BIN / name, Path(sys.executable).parent / name]
    found = shutil.which(name)
    if found:
        cands.append(Path(found))
    for c in cands:
        if c.is_file():
            return str(c)
    if url.endswith(".zip"):
        z = BIN / (name + ".zip")
        http_download(url, z, proxies)
        with zipfile.ZipFile(z) as zf:
            member = next(m for m in zf.namelist() if m.lower().endswith(name.lower()))
            with zf.open(member) as src, open(BIN / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
        z.unlink(missing_ok=True)
    else:
        http_download(url, BIN / name, proxies)
    return str(BIN / name)


def detect_ngrok_token() -> str:
    env = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    if env:
        return env
    la = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    for p in (la / "ngrok" / "ngrok.yml", Path.home() / ".config" / "ngrok" / "ngrok.yml",
              Path.home() / ".ngrok2" / "ngrok.yml"):
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith("authtoken:"):
                    v = s.split(":", 1)[1].strip().strip(chr(34) + chr(39))
                    if v:
                        return v
        except OSError:
            pass
    return ""


# ---------------------------------------------------------------- мелочи Windows

def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float, proc: subprocess.Popen) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.4)
    return False


def kill_tree(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()


def set_clipboard(text: str) -> None:
    subprocess.run(["clip"], input=text.encode("ascii", "ignore"), creationflags=NO_WINDOW, check=False)


def message_box(text: str, yes_no: bool = False) -> bool:
    if os.name != "nt":
        return True
    import ctypes
    flags = 0x40 | (0x04 if yes_no else 0) | 0x10000 | 0x40000  # INFO | YESNO | SETFOREGROUND | TOPMOST
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags) == 6


def open_path(p: Path) -> None:
    try:
        os.startfile(str(p))  # type: ignore[attr-defined]
    except OSError:
        subprocess.Popen(["notepad.exe", str(p)])


def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}" --minimized'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}" --minimized'


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, RUN_VALUE)[0])
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


_mutex = None


def single_instance() -> bool:
    global _mutex
    if os.name != "nt":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    name = "Local" + BS + "PcMcpConnect-" + hashlib.sha1(str(LOCAL).lower().encode()).hexdigest()[:12]
    _mutex = k32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


_IMAGES: dict = {}
COLORS = {"online": (46, 160, 67), "connecting": (230, 165, 0), "error": (215, 58, 73)}


def make_image(phase: str, size: int = 64):
    key = (phase, size)
    if key in _IMAGES:
        return _IMAGES[key]
    from PIL import Image, ImageDraw
    k = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (36, 38, 46)
    d.rounded_rectangle((2 * k, 2 * k, size - 3 * k, size - 3 * k), radius=14 * k, fill=bg)
    d.rectangle((21 * k, 9 * k, 27 * k, 22 * k), fill="white")
    d.rectangle((37 * k, 9 * k, 43 * k, 22 * k), fill="white")
    d.rounded_rectangle((15 * k, 21 * k, 49 * k, 40 * k), radius=7 * k, fill="white")
    d.rectangle((29 * k, 39 * k, 35 * k, 53 * k), fill="white")
    d.ellipse((37 * k, 37 * k, 62 * k, 62 * k), fill=COLORS.get(phase, COLORS["connecting"]), outline=bg,
              width=max(1, int(3 * k)))
    _IMAGES[key] = img
    return img


# ---------------------------------------------------------------- запуск MCP-сервера

class ServerManager:
    """Держит MCP-сервер запущенным на 127.0.0.1 и перезапускает его, если он упал."""

    def __init__(self, app: "App"):
        self.app = app
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.ready = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def command(self, port: int) -> list[str]:
        cfg = self.app.cfg
        base = [sys.executable, "--serve"] if FROZEN else [sys.executable, str(Path(__file__).resolve()), "--serve"]
        return base + ["--port", str(port), "--host", "127.0.0.1", "--root", cfg["root"], "--token", cfg["token"]]

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="server", daemon=True)
        self._thread.start()

    def _pump(self, proc: subprocess.Popen, token: str) -> None:
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            for line in proc.stdout:  # type: ignore[union-attr]
                f.write(line.replace(token, "***"))
                f.flush()

    def _loop(self) -> None:
        fails = 0
        while not self._stop.is_set():
            port = int(self.app.cfg.get("port") or 8770)
            if not port_free(port):
                new = free_port()
                log.warning("Порт %s занят другой программой, запускаю MCP-сервер на %s", port, new)
                port = new
            self.port = port
            root = self.app.cfg["root"]
            token = self.app.cfg["token"]
            LOCAL.mkdir(parents=True, exist_ok=True)
            with open(SERVER_LOG, "a", encoding="utf-8") as f:
                f.write(f"{NL}==== {time.strftime('%Y-%m-%d %H:%M:%S')} запуск на порту {port}{NL}")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            try:
                self.proc = subprocess.Popen(self.command(port), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                             errors="replace", env=env, creationflags=NO_WINDOW,
                                             cwd=root if os.path.isdir(root) else None)
            except Exception as e:  # noqa: BLE001
                log.error("Не удалось запустить MCP-сервер: %s", e)
                self._stop.wait(10)
                continue
            threading.Thread(target=self._pump, args=(self.proc, token), daemon=True).start()
            if wait_port(port, 60, self.proc):
                self.ready = True
                fails = 0
                log.info("MCP-сервер запущен на 127.0.0.1:%s", port)
                self.app.on_server_ready()
            while self.proc.poll() is None and not self._stop.wait(1):
                pass
            self.ready = False
            if self._stop.is_set():
                break
            fails += 1
            log.warning("MCP-сервер остановился (код %s) — перезапуск. Подробности: %s",
                        self.proc.returncode, SERVER_LOG)
            self.app.refresh()
            self._stop.wait(min(30, 2 * fails))

    def stop(self) -> None:
        self._stop.set()
        kill_tree(self.proc)
        self.ready = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)


# ---------------------------------------------------------------- способы выхода в интернет

class Connector:
    label = ""

    def __init__(self, app: "App"):
        self.app = app
        self.online = False
        self.error = False
        self.detail = "Подключаюсь…"
        self.port = 0

    def set(self, online: bool, detail: str, error: bool = False) -> None:
        changed = (online, detail, error) != (self.online, self.detail, self.error)
        self.online, self.detail, self.error = online, detail, error
        if changed:
            self.app.refresh()

    def link(self) -> str:
        return ""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class ErrorConnector(Connector):
    def __init__(self, app: "App", text: str):
        super().__init__(app)
        self.error, self.detail = True, text


class AuthError(Exception):
    pass


class RelayConnector(Connector):
    """Исходящее WebSocket-соединение с сервером-посредником; запросы Notion приходят по нему."""

    def __init__(self, app: "App", url: str, secret: str, label: str):
        super().__init__(app)
        self.url = url.strip().rstrip("/")
        self.secret = (secret or "").strip()
        self.label = label
        self.channel = channel_of(app.cfg["channel_key"])
        self.good_proxy: object = "unset"
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def link(self) -> str:
        return f"{self.url}/{self.channel}/{self.app.cfg['token']}/mcp"

    def start(self) -> None:
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="relay", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self.loop and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        try:
            await self._run()
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        delay = 3
        while not self._stopped.is_set():
            cands = await asyncio.to_thread(self.app.relay_proxy_candidates)
            if self.good_proxy in cands:
                cands.remove(self.good_proxy)
                cands.insert(0, self.good_proxy)
            connected = False
            for proxy in cands:
                if self._stopped.is_set():
                    return
                try:
                    connected = await self._session(proxy)
                except asyncio.CancelledError:
                    raise
                except AuthError as e:
                    log.warning("%s: %s", self.label, e)
                    self.set(False, str(e), error=True)
                    delay = 60
                    break
                except Exception as e:  # noqa: BLE001
                    log.info("Нет связи с %s (%s): %s", self.url, proxy or "напрямую", short_err(e))
                    self.set(False, f"Нет связи с сервером: {short_err(e)}")
                if connected:
                    break
            if connected:
                delay = 3
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _session(self, proxy: str | None) -> bool:
        import aiohttp
        connector = None
        http_proxy = None
        if proxy and proxy.startswith("socks"):
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy, rdns=True)
        elif proxy:
            http_proxy = proxy
        headers = {"X-Channel-Key": self.app.cfg["channel_key"], "X-Agent-Version": VERSION,
                   "User-Agent": f"pcmcp-connect/{VERSION}"}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as s:
            # Бесплатные хостинги засыпают — этот запрос будит сервер (может занять до минуты).
            async with s.get(self.url + "/_relay/ping", proxy=http_proxy,
                             timeout=aiohttp.ClientTimeout(total=150)) as r:
                if r.status != 200:
                    raise RuntimeError(f"сервер ответил {r.status} — проверь адрес")
            ws_url = "ws" + self.url[4:] + "/_relay/agent"
            try:
                ws = await s.ws_connect(ws_url, headers=headers, heartbeat=25, max_msg_size=MAX_MSG,
                                        proxy=http_proxy, compress=0)
            except aiohttp.WSServerHandshakeError as e:
                if e.status == 401:
                    raise AuthError("Неверный секрет своего сервера — проверь настройки") from e
                raise RuntimeError(f"сервер отказал в подключении ({e.status})") from e
            async with ws:
                hello = await ws.receive(timeout=30)
                if hello.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError("сервер не похож на PC MCP relay")
                info = json.loads(hello.data)
                if info.get("channel") != self.channel:
                    raise RuntimeError("сервер вернул чужой канал — несовместимая версия")
                self.good_proxy = proxy
                via = f" через прокси {proxy}" if proxy else ""
                log.info("На связи с %s%s", self.url, via)
                self.set(True, f"{self.label}: на связи{via}")
                await self._serve(ws, s, http_proxy)
                code = ws.close_code
            log.info("Связь с %s прервалась (код %s)", self.url, code)
            if code == 4000:
                self.set(False, "Эта ссылка уже используется на другом компьютере", error=True)
                await asyncio.sleep(30)
            else:
                self.set(False, "Связь прервалась — переподключаюсь")
            return True

    async def _serve(self, ws, s, http_proxy) -> None:
        import aiohttp
        lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task] = {}
        local = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                                      auto_decompress=False, trust_env=False)

        async def keepalive() -> None:
            while True:
                await asyncio.sleep(240)
                try:
                    async with s.get(self.url + "/_relay/ping", proxy=http_proxy,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
                        await r.read()
                except Exception:  # noqa: BLE001
                    pass

        ka = asyncio.create_task(keepalive())
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    d = json.loads(msg.data)
                except ValueError:
                    continue
                t, rid = d.get("t"), str(d.get("id"))
                if t == "req":
                    task = asyncio.create_task(self._handle(ws, lock, local, rid, d))
                    tasks[rid] = task
                    task.add_done_callback(lambda _t, rid=rid: tasks.pop(rid, None))
                elif t == "cancel":
                    task = tasks.pop(rid, None)
                    if task:
                        task.cancel()
        finally:
            ka.cancel()
            for task in list(tasks.values()):
                task.cancel()
            await local.close()

    async def _handle(self, ws, lock, local, rid: str, d: dict) -> None:
        from multidict import CIMultiDict

        async def send(obj: dict) -> None:
            if not ws.closed:
                async with lock:
                    await ws.send_str(json.dumps(obj))

        head_sent = False
        try:
            body = base64.b64decode(d.get("b") or "")
            hdrs = CIMultiDict((str(k), str(v)) for k, v in (d.get("h") or []) if str(k).lower() not in HOP)
            url = f"http://127.0.0.1:{self.app.server.port}{d.get('p') or '/'}"
            async with local.request(d.get("m") or "GET", url, headers=hdrs, data=body or None,
                                     allow_redirects=False) as r:
                await send({"t": "head", "id": rid, "s": r.status,
                            "h": [[k, v] for k, v in r.headers.items() if k.lower() not in HOP]})
                head_sent = True
                async for chunk in r.content.iter_any():
                    for i in range(0, len(chunk), CHUNK):
                        await send({"t": "chunk", "id": rid, "b": base64.b64encode(chunk[i:i + CHUNK]).decode()})
            await send({"t": "end", "id": rid})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Запрос к MCP-серверу не прошёл: %s", short_err(e))
            try:
                if head_sent:
                    await send({"t": "end", "id": rid})
                else:
                    err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message":
                           "MCP-сервер на компьютере не ответил (" + short_err(e) +
                           "). PC MCP Connect перезапускает его — повтори через несколько секунд."}}
                    await send({"t": "head", "id": rid, "s": 502, "h": [["Content-Type", "application/json"]],
                                "b": base64.b64encode(json.dumps(err, ensure_ascii=False).encode()).decode(),
                                "end": True})
            except Exception:  # noqa: BLE001
                pass


class ProcTunnel(Connector):
    """Туннель через внешнюю программу (ngrok / cloudflared)."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.proc: subprocess.Popen | None = None
        self.public = ""
        self.thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def link(self) -> str:
        return f"{self.public}/{self.app.cfg['token']}/mcp" if self.public else ""

    def build(self) -> list[str]:
        raise NotImplementedError

    def parse(self, line: str) -> str | None:
        return None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name=self.label, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stopped.set()
        kill_tree(self.proc)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        delay = 3
        while not self._stopped.is_set():
            while not self.app.server.ready and not self._stopped.wait(0.5):
                pass
            if self._stopped.is_set():
                break
            self.set(False, f"{self.label}: запускаю туннель…")
            try:
                cmd = self.build()
            except Exception as e:  # noqa: BLE001
                log.warning("%s: %s", self.label, e)
                self.set(False, f"{self.label}: {short_err(e)}", error=True)
                self._stopped.wait(30)
                continue
            started = time.time()
            self.public = ""
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                             errors="replace", creationflags=NO_WINDOW)
            except Exception as e:  # noqa: BLE001
                self.set(False, f"{self.label}: не запускается ({short_err(e)})", error=True)
                self._stopped.wait(30)
                continue
            for line in self.proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                url = self.parse(line)
                if url and not self.public:
                    self.public = url.rstrip("/")
                    log.info("%s: публичный адрес %s", self.label, self.public)
                    self.set(True, f"{self.label}: на связи")
                    self.app.on_link_changed()
            self.proc.wait()
            self.public = ""
            if self._stopped.is_set():
                break
            log.warning("%s: туннель остановился (код %s)", self.label, self.proc.returncode)
            if not self.error:
                self.set(False, f"{self.label}: туннель упал — перезапускаю")
            if time.time() - started > 120:
                delay = 3
            self._stopped.wait(delay)
            delay = min(delay * 2, 60)


class CloudflareTunnel(ProcTunnel):
    label = "Cloudflare"

    def build(self) -> list[str]:
        exe = ensure_tool("cloudflared.exe", self.app.cfg.get("cloudflared_path") or "", CLOUDFLARED_URL,
                          self.app.local_proxies())
        self.port = self.app.server.port
        return [exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{self.port}"]

    def parse(self, line: str) -> str | None:
        m = re.search(r"https://[a-z0-9-]+[.]trycloudflare[.]com", line)
        if m:
            return m.group(0)
        low = line.lower()
        if "failed to request quick tunnel" in low or "failed to unmarshal quick tunnel" in low:
            self.set(False, "Cloudflare недоступен — включи VPN (режим TUN) или выбери другой режим", error=True)
        return None


class NgrokTunnel(ProcTunnel):
    label = "ngrok"

    def __init__(self, app: "App"):
        super().__init__(app)
        self.flag = "--url"
        self.cfg_version = 3

    def build(self) -> list[str]:
        cfg = self.app.cfg
        proxies = self.app.local_proxies()
        exe = ensure_tool("ngrok.exe", cfg.get("ngrok_path") or "", NGROK_URL, proxies)
        token = (cfg.get("ngrok_authtoken") or "").strip() or detect_ngrok_token()
        if not token:
            raise RuntimeError("нужен authtoken ngrok — укажи его в настройках (dashboard.ngrok.com → Your Authtoken)")
        if self.cfg_version == 3:
            lines = ['version: "3"', "agent:", f"  authtoken: {token}"]
            if proxies:
                lines.append(f"  proxy_url: {proxies[0]}")
        else:
            lines = ['version: "2"', f"authtoken: {token}"]
            if proxies:
                lines.append(f"proxy_url: {proxies[0]}")
        LOCAL.mkdir(parents=True, exist_ok=True)
        ycfg = LOCAL / "ngrok.yml"
        ycfg.write_text(NL.join(lines) + NL, encoding="utf-8")
        self.port = self.app.server.port
        cmd = [exe, "http", f"127.0.0.1:{self.port}", "--config", str(ycfg), "--log", "stdout", "--log-format", "json"]
        domain = (cfg.get("ngrok_domain") or "").strip().replace("https://", "").strip("/")
        if domain:
            cmd += [self.flag, domain]
        if proxies:
            log.info("ngrok пойдёт через прокси %s", proxies[0])
        return cmd

    def parse(self, line: str) -> str | None:
        try:
            d = json.loads(line)
        except ValueError:
            low = line.lower()
            if "unknown flag" in low and "--url" in low:
                self.flag = "--domain"
            elif "version" in low and "config" in low and self.cfg_version == 3:
                self.cfg_version = 2
            if "err" in low:
                self.set(False, "ngrok: " + line[:150], error=True)
            return None
        if not isinstance(d, dict):
            return None
        url = str(d.get("url") or "")
        if d.get("msg") == "started tunnel" and url.startswith("https://"):
            return url
        if d.get("lvl") in ("eror", "crit") and d.get("err"):
            err = str(d.get("err")).strip().splitlines()[0][:150]
            if "ERR_NGROK" in err or "failed" in err.lower() or d.get("lvl") == "crit":
                self.set(False, "ngrok: " + err, error=True)
        return None


# ---------------------------------------------------------------- приложение

class App:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.cfg = Config()
        self.server = ServerManager(self)
        self.connector: Connector | None = None
        self.icon = None
        self._last_phase = ""
        self._last_link = ""
        self._announced = False
        self._settings_open = False
        self._switch_lock = threading.Lock()
        self.window = None  # главное окно (ui_main.MainWindow), если есть
        self.last_notice: tuple[float, str] | None = None

    # --- прокси
    def local_proxies(self, for_aiohttp: bool = False) -> list[str]:
        p = (self.cfg.get("proxy") or "auto").strip()
        if p == "off":
            return []
        found = [p] if p != "auto" else detect_proxies()
        if for_aiohttp and not has_socks():
            found = [x for x in found if x.startswith("http")]
        return found

    def relay_proxy_candidates(self) -> list:
        p = (self.cfg.get("proxy") or "auto").strip()
        if p not in ("auto", "off", ""):
            return [p]
        return [None] + self.local_proxies(for_aiohttp=True)

    # --- жизненный цикл
    def make_connector(self) -> Connector:
        mode = self.cfg["mode"]
        if mode == "shared":
            url = self.cfg.shared_url()
            if not url:
                return ErrorConnector(self, "Адрес общего сервера не задан — выбери другой режим или укажи адрес в настройках")
            return RelayConnector(self, url, "", "Общий сервер")
        if mode == "own":
            url = (self.cfg.get("own_relay_url") or "").strip()
            if not url:
                return ErrorConnector(self, "Укажи адрес своего сервера в настройках")
            return RelayConnector(self, url, self.cfg.get("own_relay_secret") or "", "Свой сервер")
        if mode == "ngrok":
            return NgrokTunnel(self)
        return CloudflareTunnel(self)

    def start(self) -> None:
        log.info("%s %s запущен, режим: %s", APP_NAME, VERSION, self.cfg["mode"])
        self.server.start()
        self.connector = self.make_connector()
        self.connector.start()
        self.refresh()
        if not self.cfg.get("first_run_done"):
            self.cfg["first_run_done"] = True
            self.cfg.save()
            if FROZEN and not self.headless:
                try:
                    set_autostart(True)
                except OSError as e:
                    log.warning("Автозапуск не включён: %s", e)
                self.notify("PC MCP Connect запущен. Если закрыть окно, программа останется в трее возле часов.")
        elif FROZEN and not self.headless and autostart_enabled():
            try:
                set_autostart(True)  # обновить путь к exe и ключ --minimized
            except OSError:
                pass
        self.write_status()

    def switch_connector(self) -> None:
        with self._switch_lock:
            old = self.connector
            if old:
                old.stop()
            self.connector = self.make_connector()
            self.connector.start()
        self.refresh()

    def quit(self) -> None:
        log.info("Выход")
        try:
            if self.connector:
                self.connector.stop()
        finally:
            self.server.stop()
        if self.icon:
            self.icon.stop()
        w = self.window
        if w is not None:
            w.close()

    # --- события
    def on_server_ready(self) -> None:
        c = self.connector
        if isinstance(c, ProcTunnel) and c.port and c.port != self.server.port:
            threading.Thread(target=self.switch_connector, daemon=True).start()
        self.refresh()

    def on_link_changed(self) -> None:
        link = self.link()
        if link and self._last_link and link != self._last_link:
            if not self.headless:
                set_clipboard(link)
            self.notify("Адрес туннеля изменился — новая ссылка скопирована. Обнови её в Notion.")
        if link:
            self._last_link = link
        self.refresh()

    # --- состояние
    def link(self) -> str:
        return self.connector.link() if self.connector else ""

    def phase(self) -> str:
        c = self.connector
        if c and c.online and self.server.ready:
            return "online"
        if c and c.error:
            return "error"
        return "connecting"

    def status_text(self) -> str:
        c = self.connector
        ph = self.phase()
        if ph == "online":
            return "● " + (c.detail if c else "")
        if ph == "error":
            return "⚠ " + (c.detail if c else "")
        if not self.server.ready:
            return "○ Запускаю MCP-сервер…"
        return "○ " + (c.detail if c else "Подключаюсь…")

    def refresh(self) -> None:
        ph = self.phase()
        if ph != self._last_phase:
            log.info("Статус: %s", self.status_text())
            self._last_phase = ph
            if ph == "online" and not self._announced:
                self._announced = True
                self.notify("Связь есть — ссылку для Notion можно копировать.")
        icon = self.icon
        if icon is not None:
            try:
                icon.icon = make_image(ph)
                icon.title = (APP_NAME + " — " + self.status_text())[:120]
                icon.update_menu()
            except Exception:  # noqa: BLE001
                pass
        self.write_status()

    def write_status(self) -> None:
        if not self.headless:
            return
        try:
            (LOCAL / "status.json").write_text(json.dumps({"phase": self.phase(), "status": self.status_text(),
                                                          "link": self.link(), "port": self.server.port},
                                                         ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def notify(self, text: str) -> None:
        log.info("Уведомление: %s", text)
        self.last_notice = (time.time(), text)
        if self.icon is not None:
            try:
                self.icon.notify(text, APP_NAME)
            except Exception:  # noqa: BLE001
                pass

    # --- действия из меню
    def copy_link(self, silent: bool = False) -> None:
        link = self.link()
        if not link:
            self.notify("Ссылки пока нет — подожди, пока появится связь.")
            return
        set_clipboard(link)
        self._last_link = link
        if not silent:
            self.notify("Ссылка скопирована. Вставь её в Notion как адрес MCP-сервера, Authentication: None.")

    def check_link(self) -> tuple[bool, str]:
        link = self.link()
        if not link:
            return False, "Ссылки пока нет — подожди, пока появится связь."
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "pcmcp-connect-check", "version": VERSION}}}).encode()
        last = ""
        for proxy in [None] + [p for p in self.local_proxies() if p.startswith("http")]:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
            req = urllib.request.Request(link, data=body, method="POST", headers={
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "User-Agent": f"pcmcp-connect/{VERSION}"})
            try:
                with opener.open(req, timeout=60) as r:
                    txt = r.read(200000).decode("utf-8", "replace")
                    if r.status == 200 and "serverInfo" in txt:
                        return True, "Ссылка работает — Notion сможет подключиться."
                    last = f"неожиданный ответ ({r.status})"
            except urllib.error.HTTPError as e:
                txt = e.read(4000).decode("utf-8", "replace")
                msg = ""
                try:
                    msg = json.loads(txt).get("error", {}).get("message", "")
                except Exception:  # noqa: BLE001
                    pass
                return False, f"Ошибка {e.code}: {msg or txt[:150]}"
            except Exception as e:  # noqa: BLE001
                last = short_err(e)
        return False, "Не удалось открыть ссылку: " + last

    def check_async(self) -> None:
        def run() -> None:
            ok, msg = self.check_link()
            self.notify(("✅ " if ok else "❌ ") + msg)
        threading.Thread(target=run, daemon=True).start()

    def set_mode(self, mode: str) -> None:
        if mode == self.cfg["mode"] or mode not in MODES:
            return
        self.cfg["mode"] = mode
        self.cfg.save()
        self._announced = False

        def run() -> None:
            self.switch_connector()
            if self.link():
                self.copy_link(silent=True)
                self.notify("Режим изменён. Ссылка скопирована — обнови её в Notion.")
        threading.Thread(target=run, daemon=True).start()

    def regenerate(self) -> None:
        if not message_box("Создать новую ссылку?" + NL + NL + "Старая перестанет работать, новую нужно будет вставить в Notion.",
                           yes_no=True):
            return
        self.cfg["token"] = secrets.token_urlsafe(24)
        self.cfg.save()
        self.server.stop()
        self.server.start()
        self.copy_link(silent=True)
        self.notify("Новая ссылка создана и скопирована. Обнови её в Notion.")

    def toggle_autostart(self) -> None:
        try:
            set_autostart(not autostart_enabled())
        except OSError as e:
            self.notify(f"Не удалось изменить автозапуск: {e}")
        if self.icon is not None:
            self.icon.update_menu()

    def apply_settings(self, new: dict) -> None:
        server_changed = new.get("root") != self.cfg.get("root")
        keys = ("mode", "shared_relay_url", "own_relay_url", "own_relay_secret", "ngrok_authtoken", "ngrok_domain",
                "ngrok_path", "proxy")
        conn_changed = any(new.get(k) != self.cfg.get(k) for k in keys)
        old_link = self.link()
        self.cfg.update(new)
        self.cfg.save()
        reset_proxy_cache()
        if server_changed:
            self.server.stop()
            self.server.start()
        if conn_changed:
            self._announced = False
            self.switch_connector()
        link = self.link()
        if link and link != old_link:
            self.copy_link(silent=True)
            self.notify("Настройки сохранены. Новая ссылка скопирована — обнови её в Notion.")
        else:
            self.notify("Настройки сохранены.")

    def open_settings(self) -> None:
        w = self.window
        if w is not None:
            w.call(w.open_settings)
            return
        if self._settings_open:
            return
        self._settings_open = True
        threading.Thread(target=self._settings_window, name="settings", daemon=True).start()

    def _settings_window(self, parent=None):
        try:
            import tkinter as tk
            from tkinter import filedialog, ttk
            cfg = self.cfg
            root = tk.Toplevel(parent) if parent is not None else tk.Tk()
            root.title(APP_NAME + " — настройки")
            if parent is None:
                sty = ttk.Style(root)
                sty.configure("Hint.TLabel", foreground="#666")
                sty.configure("Err.TLabel", foreground="#c00")
            root.resizable(False, False)
            root.attributes("-topmost", True)
            keys = ("mode", "own_relay_url", "own_relay_secret", "ngrok_authtoken", "ngrok_domain", "ngrok_path",
                    "proxy", "root")
            v = {k: tk.StringVar(value=str(cfg.get(k) or "")) for k in keys}
            v["shared_relay_url"] = tk.StringVar(value=cfg.shared_url())
            f = ttk.Frame(root, padding=14)
            f.grid(sticky="nsew")
            row = [0]

            def nxt() -> int:
                row[0] += 1
                return row[0] - 1

            ttk.Label(f, text="Как подключаться к Notion:", font=("Segoe UI", 10, "bold")).grid(
                row=nxt(), column=0, columnspan=3, sticky="w")
            rf = ttk.Frame(f)
            rf.grid(row=nxt(), column=0, columnspan=3, sticky="w", padx=8, pady=(4, 0))
            for i, (m, title) in enumerate(MODES.items()):
                ttk.Radiobutton(rf, text=title, value=m, variable=v["mode"]).grid(
                    row=i // 2, column=i % 2, sticky="w", padx=(0, 24))
            line_bg = getattr(self.window, "line_color", None) or "#d0d0d0"

            def section(title: str) -> None:
                tk.Frame(f, height=1, bg=line_bg).grid(row=nxt(), column=0, columnspan=3, sticky="ew", pady=(10, 5))
                ttk.Label(f, text=title, font=("Segoe UI", 9, "bold")).grid(row=nxt(), column=0, columnspan=3,
                                                                             sticky="w")

            def field(label: str, key: str, show: str = "", button=None) -> None:
                r = nxt()
                ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=(8, 6), pady=2)
                ttk.Entry(f, textvariable=v[key], width=50, show=show, font=("Segoe UI", 10)).grid(row=r, column=1, sticky="ew", pady=2)
                if button:
                    ttk.Button(f, text=button[0], command=button[1]).grid(row=r, column=2, padx=(6, 0), sticky="ew")

            def hint(text: str) -> None:
                ttk.Label(f, text=text, style="Hint.TLabel", wraplength=500).grid(row=nxt(), column=1, columnspan=2,
                                                                              sticky="w")

            section("Общий сервер")
            field("Адрес:", "shared_relay_url")
            section("Свой сервер (Hugging Face Space или VPS)")
            field("Адрес:", "own_relay_url", button=("Инструкция…", lambda: open_path(RES / "README.md")))
            field("Секрет:", "own_relay_secret", show="•",
                  button=("Создать", lambda: v["own_relay_secret"].set(secrets.token_urlsafe(32))))
            hint("Секрет нужен, только если ты задал RELAY_SECRET на своём сервере.")
            section("ngrok")
            field("Authtoken:", "ngrok_authtoken", show="•")
            field("Свой домен:", "ngrok_domain")
            field("ngrok.exe:", "ngrok_path", button=("Обзор…", lambda: v["ngrok_path"].set(
                filedialog.askopenfilename(parent=root, filetypes=[("ngrok", "ngrok.exe")]) or v["ngrok_path"].get())))
            hint("Пустые поля — возьму токен из установленного ngrok и скачаю его сам.")
            section("Прочее")
            field("Прокси:", "proxy")
            hint("auto — найти прокси VPN, off — без прокси, или адрес http://127.0.0.1:10809")
            field("Папка по умолчанию:", "root", button=("Обзор…", lambda: v["root"].set(
                filedialog.askdirectory(parent=root) or v["root"].get())))
            err = ttk.Label(f, text="", style="Err.TLabel")
            err.grid(row=nxt(), column=0, columnspan=3, sticky="w")

            def save() -> None:
                new = {k: var.get().strip() for k, var in v.items()}
                for k in ("shared_relay_url", "own_relay_url"):
                    new[k] = new[k].rstrip("/")
                    if new[k] and not re.match(r"^https?://[^/ ]+$", new[k]):
                        err.config(text="Адрес сервера должен выглядеть так: https://имя-сервера.hf.space")
                        return
                if new["mode"] == "own" and not new["own_relay_url"]:
                    err.config(text="Укажи адрес своего сервера")
                    return
                if new["mode"] == "shared" and not new["shared_relay_url"]:
                    err.config(text="Укажи адрес общего сервера или выбери другой режим")
                    return
                if not os.path.isdir(new["root"]):
                    err.config(text="Папка не найдена")
                    return
                if new["shared_relay_url"] == (cfg.defaults.get("shared_relay_url") or "").rstrip("/"):
                    new["shared_relay_url"] = ""
                new["proxy"] = new["proxy"] or "auto"
                root.destroy()
                threading.Thread(target=self.apply_settings, args=(new,), daemon=True).start()

            btns = ttk.Frame(f)
            btns.grid(row=nxt(), column=0, columnspan=3, sticky="e", pady=(10, 0))
            ttk.Button(btns, text="Отмена", command=root.destroy).pack(side="right")
            ttk.Button(btns, text="Сохранить", command=save, style="Accent.TButton").pack(side="right", padx=6)
            root.after(300, lambda: root.attributes("-topmost", False))
            if parent is not None:
                return root
            root.mainloop()
        except Exception:  # noqa: BLE001
            log.exception("Окно настроек")
        finally:
            self._settings_open = False


# ---------------------------------------------------------------- значок в трее

def run_tray(app: App) -> None:
    import pystray
    from pystray import Menu
    from pystray import MenuItem as Item

    def noop(icon, item) -> None:
        pass

    def mode_item(m: str):
        return Item(MODES[m], lambda icon, item: app.set_mode(m), checked=lambda item: app.cfg["mode"] == m,
                    radio=True)

    def in_thread(fn):
        return lambda icon, item: threading.Thread(target=fn, daemon=True).start()

    menu = Menu(
        Item(lambda item: app.status_text()[:100], noop, enabled=False),
        Item(lambda item: "Ссылка: " + (mask_link(app.link(), app.cfg["token"])[:90] or "пока нет"), noop, enabled=False),
        Menu.SEPARATOR,
        Item("Скопировать ссылку для Notion", lambda icon, item: app.copy_link(), default=True),
        Item("Проверить ссылку", lambda icon, item: app.check_async()),
        Menu.SEPARATOR,
        Item("Режим подключения", Menu(*[mode_item(m) for m in MODES])),
        Item("Настройки…", lambda icon, item: app.open_settings()),
        Item("Новая ссылка (сменить ключ доступа)", in_thread(app.regenerate)),
        Item("Запускать вместе с Windows", lambda icon, item: app.toggle_autostart(),
             checked=lambda item: autostart_enabled()),
        Item("Открыть журнал", lambda icon, item: open_path(LOG_FILE)),
        Menu.SEPARATOR,
        Item("Выход", in_thread(app.quit)),
    )
    icon = pystray.Icon("PcMcpConnect", make_image("connecting"), APP_NAME, menu)
    app.icon = icon

    def setup(ic) -> None:
        ic.visible = True
        app.start()

    icon.run(setup=setup)


# ---------------------------------------------------------------- точка входа

def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["--serve"]:
        serve_main(argv[1:])
        return
    headless = "--headless" in argv
    setup_logging(console=headless or not FROZEN)
    if not single_instance():
        if headless:
            log.error("Уже запущено")
        else:
            shown = False
            try:
                import ui_main
                shown = ui_main.signal_existing(sys.modules[__name__])
            except Exception:  # noqa: BLE001
                pass
            if not shown:
                message_box("PC MCP Connect уже запущен — значок возле часов (возможно, в скрытых значках).")
        return
    app = App(headless=headless)
    if not headless:
        try:
            import ui_main
        except Exception:  # noqa: BLE001
            log.exception("Окно недоступно — работаю только в трее")
            run_tray(app)
            return
        ui_main.run(app, sys.modules[__name__], start_hidden="--minimized" in argv)
        return
    seconds = 0.0
    if "--seconds" in argv:
        seconds = float(argv[argv.index("--seconds") + 1])
    end = time.time() + seconds if seconds else None
    app.start()
    try:
        while end is None or time.time() < end:
            time.sleep(2)
            app.write_status()
    except KeyboardInterrupt:
        pass
    finally:
        app.quit()


if __name__ == "__main__":
    main()
