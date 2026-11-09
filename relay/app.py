"""PC MCP relay для бесплатного Hugging Face Space (SDK Gradio, оборудование ZeroGPU).

Hugging Face запускает app.py. Здесь поднимается маленькая страница Gradio, а в тот же
веб-сервер добавляются маршруты relay:

    /_relay/ping, /_relay/status/<канал>, /_relay/agent (WebSocket), /<канал>/<токен>/mcp

GPU не используется. Функция с @spaces.GPU нужна только потому, что без неё ZeroGPU-Space
не запускается; она никогда не вызывается. Протокол тот же, что в relay.py
(relay.py — для своего VPS или Docker).
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import itertools
import json
import logging
import os
import time

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

try:
    import spaces  # на Hugging Face есть всегда; импортировать до gradio
except Exception:  # noqa: BLE001
    spaces = None

import gradio as gr  # noqa: E402
from starlette.convertors import Convertor, register_url_convertor  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse  # noqa: E402
from starlette.routing import Route, WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocket, WebSocketDisconnect  # noqa: E402

from relay import (ANY_PATH, HOP, MAX_BODY, MAX_CHANNELS, MAX_PENDING, MCP_TAIL_RE, OFFLINE,  # noqa: E402
                   SECRET, STRIP, TIMEOUT, VERSION, channel_of, short)

PORT = int(os.environ.get("GRADIO_SERVER_PORT") or os.environ.get("PORT") or 7860)
NO_STORE = {"Cache-Control": "no-store"}
log = logging.getLogger("pcmcp-relay")
IDS = itertools.count(1)

if spaces is not None:
    @spaces.GPU(duration=1)
    def _zerogpu_placeholder() -> None:
        """Никогда не вызывается: нужна только чтобы ZeroGPU разрешил запуск."""
        return None


class ChannelConvertor(Convertor):
    """Канал — ровно 20 символов base32, поэтому не пересекается с путями Gradio."""
    regex = "[a-z2-7]{20}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("pcmcpch", ChannelConvertor())


class Agent:
    """Подключённый компьютер: WebSocket и запросы, которые ждут его ответа."""

    def __init__(self, ws: WebSocket, channel: str, version: str):
        self.ws = ws
        self.channel = channel
        self.version = version
        self.since = time.time()
        self.pending: dict[str, asyncio.Queue] = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def send(self, data: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False))

    def fail_all(self) -> None:
        for q in self.pending.values():
            q.put_nowait({"t": "gone"})
        self.pending.clear()


CHANNELS: dict[str, Agent] = {}


def rpc_error(status: int, text: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": text}},
                        status_code=status, headers=NO_STORE)


async def ping(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", headers=NO_STORE)


async def status(request: Request) -> JSONResponse:
    a = CHANNELS.get(request.path_params["ch"])
    return JSONResponse({"app": "pcmcp-relay", "relay": VERSION, "private": bool(SECRET), "online": a is not None,
                         "connected_for": int(time.time() - a.since) if a else None}, headers=NO_STORE)


async def deny(ws: WebSocket, code: int, error: str) -> None:
    try:
        await ws.send_denial_response(JSONResponse({"error": error}, status_code=code))
    except Exception:  # noqa: BLE001 — сервер не умеет отвечать HTTP-ошибкой на WebSocket
        try:
            await ws.close(code=1008)
        except Exception:  # noqa: BLE001
            pass


async def agent_ws(ws: WebSocket) -> None:
    if SECRET:
        h = ws.headers.get("authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not hmac.compare_digest(tok.encode(), SECRET.encode()):
            await asyncio.sleep(1)
            await deny(ws, 401, "wrong secret")
            return
    key = ws.headers.get("x-channel-key", "").strip()
    if len(key) < 32:
        await deny(ws, 400, "channel key is too short")
        return
    ch = channel_of(key)
    if ch not in CHANNELS and len(CHANNELS) >= MAX_CHANNELS:
        await deny(ws, 503, "relay is full")
        return
    await ws.accept()
    agent = Agent(ws, ch, ws.headers.get("x-agent-version", "?")[:32])
    old = CHANNELS.get(ch)
    CHANNELS[ch] = agent
    if old is not None:
        old.closed = True
        old.fail_all()
        try:
            await old.ws.close(code=4000, reason="replaced by a newer connection")
        except Exception:  # noqa: BLE001
            pass
    log.info("канал %s: компьютер подключился (версия %s), всего на связи: %d",
             short(ch), agent.version, len(CHANNELS))
    try:
        await agent.send({"t": "welcome", "relay": VERSION, "channel": ch, "private": bool(SECRET)})
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if not text:
                continue
            try:
                d = json.loads(text)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            q = agent.pending.get(str(d.get("id")))
            if q is not None and d.get("t") in ("head", "chunk", "end"):
                q.put_nowait(d)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        agent.closed = True
        if CHANNELS.get(ch) is agent:
            del CHANNELS[ch]
            log.info("канал %s: компьютер отключился", short(ch))
        agent.fail_all()


async def _send_cancel(agent: Agent, rid: str) -> None:
    try:
        await agent.send({"t": "cancel", "id": rid})
    except Exception:  # noqa: BLE001
        pass


async def forward(request: Request):
    ch, tail = request.path_params["ch"], request.path_params["tail"]
    if not ANY_PATH and not MCP_TAIL_RE.match(tail):
        return PlainTextResponse("not found", status_code=404)
    agent = CHANNELS.get(ch)
    if agent is None or agent.closed:
        return rpc_error(503, OFFLINE)
    if len(agent.pending) >= MAX_PENDING:
        return rpc_error(503, "Слишком много одновременных запросов к компьютеру — повтори чуть позже.")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        return rpc_error(413, "Слишком большой запрос.")
    body = bytearray()
    async for part in request.stream():
        body += part
        if len(body) > MAX_BODY:
            return rpc_error(413, "Слишком большой запрос.")
    headers = [[k, v] for k, v in request.headers.items()
               if k.lower() not in HOP and not k.lower().startswith(STRIP)]
    query = request.url.query
    path = "/" + tail + ("?" + query if query else "")
    rid = str(next(IDS))
    q: asyncio.Queue = asyncio.Queue()
    agent.pending[rid] = q
    t0 = time.monotonic()
    state = {"done": False, "status": "-", "cleaned": False}

    def cleanup() -> None:
        if state["cleaned"]:
            return
        state["cleaned"] = True
        agent.pending.pop(rid, None)
        if not state["done"] and not agent.closed:
            asyncio.get_running_loop().create_task(_send_cancel(agent, rid))
        log.info("канал %s: %s → %s, %.0f мс", short(ch), request.method, state["status"],
                 (time.monotonic() - t0) * 1000)

    try:
        await agent.send({"t": "req", "id": rid, "m": request.method, "p": path, "h": headers,
                          "b": base64.b64encode(bytes(body)).decode()})
        try:
            first = await asyncio.wait_for(q.get(), TIMEOUT)
        except asyncio.TimeoutError:
            state["status"] = 504
            return rpc_error(504, f"Компьютер не ответил за {int(TIMEOUT)} с.")
        if first.get("t") != "head":
            state["done"] = True
            state["status"] = 502
            return rpc_error(502, "Компьютер отключился, не успев ответить. Повтори через несколько секунд.")
    except Exception:
        cleanup()
        raise
    finally:
        if state["status"] != "-":
            cleanup()

    code = int(first.get("s") or 502)
    state["status"] = code

    async def body_iter():
        msg = first
        try:
            while True:
                if msg.get("b"):
                    yield base64.b64decode(msg["b"])
                if msg.get("t") in ("end", "gone") or msg.get("end"):
                    state["done"] = True
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), TIMEOUT)
                except asyncio.TimeoutError:
                    break
        finally:
            cleanup()

    resp = StreamingResponse(body_iter(), status_code=code)
    for item in first.get("h") or []:
        if isinstance(item, list) and len(item) == 2 and str(item[0]).lower() not in HOP:
            resp.raw_headers.append((str(item[0]).lower().encode("latin-1", "replace"),
                                     str(item[1]).encode("latin-1", "replace")))
    resp.raw_headers.append((b"x-accel-buffering", b"no"))
    return resp


RELAY_ROUTES = [
    Route("/_relay/ping", ping, methods=["GET"]),
    Route("/_relay/status/{ch}", status, methods=["GET"]),
    WebSocketRoute("/_relay/agent", agent_ws),
    Route("/{ch:pcmcpch}/{tail:path}", forward, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
]


def page_text() -> str:
    mode = "личный (нужен секрет)" if SECRET else "общий"
    return f"""## 🔌 PC MCP relay {VERSION}

Сервер работает. Режим: **{mode}**. Компьютеров на связи: **{len(CHANNELS)}**.

Это сервер-посредник для приложения **PC MCP Connect**: оно даёт MCP-серверу на домашнем компьютере
постоянную HTTPS-ссылку для Notion — без ngrok, Cloudflare и белого IP.

Сервер ничего не хранит и только пересылает запросы. GPU не используется.
"""


with gr.Blocks(title="PC MCP relay") as demo:
    info = gr.Markdown(page_text())
    demo.load(page_text, None, info)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    log.info("PC MCP relay %s (Gradio), порт %d, режим: %s", VERSION, PORT, "личный" if SECRET else "общий")
    demo.queue()
    demo.launch(server_name="0.0.0.0", server_port=PORT, ssr_mode=False, prevent_thread_lock=True)
    app = getattr(demo, "server_app", None) or demo.app
    for route in reversed(RELAY_ROUTES):
        app.router.routes.insert(0, route)
    log.info("маршруты relay добавлены")
    demo.block_thread()


if __name__ == "__main__":
    main()
