#!/usr/bin/env python3
"""PC MCP relay — постоянный адрес для MCP-сервера на домашнем компьютере.

    Notion ──HTTPS──▶ relay (этот сервер) ◀──WebSocket── PC MCP Connect на компьютере

Компьютер сам подключается к relay исходящим соединением, поэтому не нужны белый IP, проброс
портов, ngrok и Cloudflare. Адрес для Notion не меняется:

    https://<relay>/<канал>/<токен>/mcp

Канал — хеш от секретного ключа канала, который знает только компьютер. Поэтому занять чужой
канал нельзя, а серверу не нужно ничего хранить (перезапуски хостинга не страшны).
Токен в ссылке проверяет сам MCP-сервер на компьютере; relay его не записывает в журнал.

Переменные окружения:
  PORT             порт (Hugging Face Spaces: 7860, по умолчанию 7860)
  RELAY_SECRET     если задан — личный сервер: подключаться могут только компьютеры с этим
                   секретом. Если пуст — общий сервер: подключиться может любой, каждый в свой канал.
  REQUEST_TIMEOUT  сколько ждать ответа компьютера, секунд (по умолчанию 300)
  MAX_CHANNELS     сколько компьютеров может быть на связи одновременно (по умолчанию 500)
  RELAY_ANY_PATH   1 — пропускать любые пути, а не только /<токен>/mcp (по умолчанию выключено)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import logging
import os
import re
import time

from aiohttp import WSMsgType, web

VERSION = "1.0"
PORT = int(os.environ.get("PORT") or 7860)
SECRET = os.environ.get("RELAY_SECRET", "").strip()
TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT") or 300)
MAX_CHANNELS = int(os.environ.get("MAX_CHANNELS") or 500)
ANY_PATH = os.environ.get("RELAY_ANY_PATH", "") == "1"
MAX_BODY = 32 * 1024 * 1024
MAX_MSG = 64 * 1024 * 1024
MAX_PENDING = 32
CHANNEL_RE = re.compile(r"^[a-z2-7]{20}$")
MCP_TAIL_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}/mcp/?$")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
STRIP = ("x-forwarded-", "x-real-ip", "forwarded", "x-relay-", "cf-", "true-client-ip", "x-amzn-", "via",
         "x-request-id", "x-direct-url", "x-ip-token")
OFFLINE = ("Компьютер не в сети: PC MCP Connect на нём не запущен или ещё не подключился. "
           "Запусти его (или подожди минуту) и повтори.")

log = logging.getLogger("pcmcp-relay")
IDS = itertools.count(1)
STARTED = time.time()


def channel_of(key: str) -> str:
    """Канал из ключа канала (та же функция есть в приложении)."""
    digest = hashlib.sha256(("pcmcp-channel:" + key).encode()).digest()
    return base64.b32encode(digest).decode().lower().rstrip("=")[:20]


class Agent:
    """Подключённый компьютер: WebSocket и запросы, которые ждут его ответа."""

    def __init__(self, ws: web.WebSocketResponse, channel: str, version: str):
        self.ws = ws
        self.channel = channel
        self.version = version
        self.since = time.time()
        self.pending: dict[str, asyncio.Queue] = {}
        self.lock = asyncio.Lock()

    async def send(self, data: dict) -> None:
        async with self.lock:
            await self.ws.send_str(json.dumps(data, ensure_ascii=False))

    def fail_all(self) -> None:
        for q in self.pending.values():
            q.put_nowait({"t": "gone"})
        self.pending.clear()


CHANNELS: dict[str, Agent] = {}


def short(ch: str) -> str:
    return ch[:5] + "…"


def rpc_error(status: int, text: str) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": text}},
                             status=status, headers={"Cache-Control": "no-store"})


async def index(request: web.Request) -> web.Response:
    mode = "личный" if SECRET else "общий"
    return web.Response(text=f"PC MCP relay {VERSION} работает. Режим: {mode}. Компьютеров на связи: {len(CHANNELS)}.\n",
                        headers={"Cache-Control": "no-store"})


async def ping(request: web.Request) -> web.Response:
    return web.Response(text="ok", headers={"Cache-Control": "no-store"})


async def status(request: web.Request) -> web.Response:
    ch = request.match_info["ch"]
    a = CHANNELS.get(ch)
    return web.json_response({"app": "pcmcp-relay", "relay": VERSION, "private": bool(SECRET), "online": a is not None,
                              "connected_for": int(time.time() - a.since) if a else None},
                             headers={"Cache-Control": "no-store"})


async def agent_ws(request: web.Request) -> web.StreamResponse:
    if SECRET:
        h = request.headers.get("Authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not hmac.compare_digest(tok.encode(), SECRET.encode()):
            await asyncio.sleep(1)
            return web.json_response({"error": "wrong secret"}, status=401)
    key = request.headers.get("X-Channel-Key", "").strip()
    if len(key) < 32:
        return web.json_response({"error": "channel key is too short"}, status=400)
    ch = channel_of(key)
    if ch not in CHANNELS and len(CHANNELS) >= MAX_CHANNELS:
        return web.json_response({"error": "relay is full"}, status=503)
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=MAX_MSG, compress=False)
    await ws.prepare(request)
    agent = Agent(ws, ch, request.headers.get("X-Agent-Version", "?")[:32])
    old = CHANNELS.get(ch)
    CHANNELS[ch] = agent
    if old is not None:
        old.fail_all()
        await old.ws.close(code=4000, message=b"replaced by a newer connection")
    log.info("канал %s: компьютер подключился (версия %s), всего на связи: %d",
             short(ch), agent.version, len(CHANNELS))
    try:
        await agent.send({"t": "welcome", "relay": VERSION, "channel": ch, "private": bool(SECRET)})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type == WSMsgType.ERROR:
                    break
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            q = agent.pending.get(str(d.get("id")))
            if q is not None and d.get("t") in ("head", "chunk", "end"):
                q.put_nowait(d)
    finally:
        if CHANNELS.get(ch) is agent:
            del CHANNELS[ch]
            log.info("канал %s: компьютер отключился", short(ch))
        agent.fail_all()
    return ws


async def forward(request: web.Request) -> web.StreamResponse:
    ch, tail = request.match_info["ch"], request.match_info["tail"]
    if not CHANNEL_RE.match(ch) or (not ANY_PATH and not MCP_TAIL_RE.match(tail)):
        return web.Response(status=404, text="not found\n")
    agent = CHANNELS.get(ch)
    if agent is None or agent.ws.closed:
        return rpc_error(503, OFFLINE)
    if len(agent.pending) >= MAX_PENDING:
        return rpc_error(503, "Слишком много одновременных запросов к компьютеру — повтори чуть позже.")
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return rpc_error(413, "Слишком большой запрос.")
    headers = [[k, v] for k, v in request.headers.items()
               if k.lower() not in HOP and not k.lower().startswith(STRIP)]
    path = "/" + tail + ("?" + request.query_string if request.query_string else "")
    rid = str(next(IDS))
    q: asyncio.Queue = asyncio.Queue()
    agent.pending[rid] = q
    t0 = time.monotonic()
    done = False
    resp: web.StreamResponse | None = None
    try:
        await agent.send({"t": "req", "id": rid, "m": request.method, "p": path, "h": headers,
                          "b": base64.b64encode(body).decode()})
        try:
            first = await asyncio.wait_for(q.get(), TIMEOUT)
        except asyncio.TimeoutError:
            return rpc_error(504, f"Компьютер не ответил за {int(TIMEOUT)} с.")
        if first.get("t") != "head":
            done = True
            return rpc_error(502, "Компьютер отключился, не успев ответить. Повтори через несколько секунд.")
        resp = web.StreamResponse(status=int(first.get("s") or 502))
        for item in first.get("h") or []:
            if isinstance(item, list) and len(item) == 2 and str(item[0]).lower() not in HOP:
                resp.headers.add(str(item[0]), str(item[1]))
        resp.headers["X-Accel-Buffering"] = "no"
        await resp.prepare(request)
        msg = first
        while True:
            if msg.get("b"):
                await resp.write(base64.b64decode(msg["b"]))
            if msg.get("t") in ("end", "gone") or msg.get("end"):
                done = True
                break
            try:
                msg = await asyncio.wait_for(q.get(), TIMEOUT)
            except asyncio.TimeoutError:
                break
        await resp.write_eof()
        return resp
    finally:
        agent.pending.pop(rid, None)
        if not done and not agent.ws.closed:
            try:
                await agent.send({"t": "cancel", "id": rid})
            except Exception:  # noqa: BLE001
                pass
        log.info("канал %s: %s → %s, %.0f мс", short(ch), request.method,
                 resp.status if resp is not None else "-", (time.monotonic() - t0) * 1000)


def make_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/", index)
    app.router.add_get("/_relay/ping", ping)
    app.router.add_get("/_relay/status/{ch}", status)
    app.router.add_get("/_relay/agent", agent_ws)
    app.router.add_route("*", "/{ch}/{tail:.*}", forward)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    log.info("PC MCP relay %s, порт %d, режим: %s", VERSION, PORT, "личный" if SECRET else "общий")
    web.run_app(make_app(), host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
