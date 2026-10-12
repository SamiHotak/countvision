"""Small local HTTP servers for tests: a fake Telegram Bot API and a webhook receiver."""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD_TOKEN = "123456789:AAtest-token-for-countvision"


class _Server:
    def __init__(self, handler_cls) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _TelegramHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        owner: FakeTelegram = self.server.owner  # type: ignore[attr-defined]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        _, bot, method = self.path.split("/", 2)
        token = bot.removeprefix("bot")
        if token != GOOD_TOKEN:
            return self._send(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if method == "getMe":
            return self._send(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "cv_test_bot"}})
        if method == "getUpdates":
            return self._send(200, {"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 4242, "type": "private", "first_name": "Ezat"}}},
                {"update_id": 2, "message": {"chat": {"id": -100777, "type": "group", "title": "Shop team"}}},
            ]})
        if method == "sendMessage":
            if body.get("chat_id") not in (4242, -100777):
                return self._send(400, {"ok": False, "description": "Bad Request: chat not found"})
            owner.messages.append(body)
            return self._send(200, {"ok": True, "result": {"message_id": len(owner.messages)}})
        return self._send(404, {"ok": False, "description": "Not Found"})

    def _send(self, code: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeTelegram(_Server):
    def __init__(self) -> None:
        self.messages: list[dict] = []
        super().__init__(_TelegramHandler)


class _HookHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        owner: WebhookReceiver = self.server.owner  # type: ignore[attr-defined]
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        code = owner.codes.popleft() if owner.codes else 200
        owner.requests.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body,
                               "json": json.loads(body), "code": code})
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class WebhookReceiver(_Server):
    """Answers 200, or the codes put into ``codes`` first (one per request)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.codes: deque[int] = deque()
        super().__init__(_HookHandler)

    def of_type(self, event_type: str) -> list[dict]:
        return [r for r in self.requests if r["json"]["type"] == event_type]
