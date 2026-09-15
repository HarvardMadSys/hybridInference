"""Verify that the Uvicorn boundary preserves the socket peer for resolution."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import uvicorn
from starlette.requests import Request

from serving.utils.request_ip import get_client_ip_info


async def _app(scope, receive, send):
    """Expose resolver output from a minimal ASGI application."""
    if scope["type"] != "http":
        return

    request = Request(scope, receive)
    info = get_client_ip_info(request)
    body = json.dumps(
        {
            "client_ip": info.client_ip,
            "peer_ip": info.peer_ip,
            "source": info.source,
            "trusted_proxy_headers": info.trusted_proxy_headers,
            "ip_resolved": info.resolved,
            "trusted_forwarded_headers": info.trusted_forwarded_headers,
            "trusted_cloudflare_headers": info.trusted_cloudflare_headers,
            "x_forwarded_for": info.x_forwarded_for,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_uvicorn_no_proxy_headers_preserves_socket_peer():
    """Forwarding headers cannot rewrite the peer used by the resolver."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            _app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            lifespan="off",
            proxy_headers=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        for _ in range(500):
            if server.started or not thread.is_alive():
                break
            time.sleep(0.01)
        assert server.started

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                "GET",
                "/",
                headers={
                    "X-Forwarded-For": "8.8.8.8",
                    "X-Real-IP": "1.1.1.1",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
        finally:
            connection.close()

        assert response.status == 200
        assert payload["peer_ip"] == "127.0.0.1"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_uvicorn_preserves_duplicate_xff_field_lines():
    """The ASGI boundary preserves all physical XFF fields for the resolver."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            _app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            lifespan="off",
            proxy_headers=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        for _ in range(500):
            if server.started or not thread.is_alive():
                break
            time.sleep(0.01)
        assert server.started

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.putrequest("GET", "/")
            connection.putheader("X-Forwarded-For", "8.8.8.8")
            connection.putheader("x-forwarded-for", "1.1.1.1")
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read())
        finally:
            connection.close()

        assert response.status == 200
        assert payload["peer_ip"] == "127.0.0.1"
        assert payload["x_forwarded_for"] == "8.8.8.8, 1.1.1.1"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()
