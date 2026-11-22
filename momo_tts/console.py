"""Bind the local console and its selected LAN interface in one server."""
from contextlib import ExitStack
import os
import socket

import uvicorn

from .network import CONSOLE_PORT, console_address


def main():
    host = os.environ.get("MOMO_TTS_ADMIN_LAN", "")
    if host:
        console_address(host)
    addresses = ("127.0.0.1", host) if host else ("127.0.0.1",)
    config = uvicorn.Config(
        "momo_tts.admin:create_app", factory=True,
        proxy_headers=False, access_log=False, timeout_graceful_shutdown=10,
    )
    with ExitStack() as resources:
        listeners = []
        for address in addresses:
            listener = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((address, CONSOLE_PORT))
            listener.listen(config.backlog)
            listeners.append(listener)
        uvicorn.Server(config).run(sockets=listeners)


if __name__ == "__main__":
    main()
