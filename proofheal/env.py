"""Runtime environment: demo shop server process and a network-scoped browser."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Route, sync_playwright

LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def shop_server(variant: str) -> Iterator[str]:
    """Start demo_app with SHOP_VARIANT=variant on a free local port; yield its base URL."""
    port = _free_port()
    env = {**os.environ, "SHOP_VARIANT": variant, "PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "demo_app.app"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError(f"demo shop exited: {proc.stderr.read().decode()[-500:]}")
            try:
                urllib.request.urlopen(base + "/healthz", timeout=0.5)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("demo shop did not start within 10s")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _local_only(route: Route) -> None:
    host = urlparse(route.request.url).hostname or ""
    if host in LOCAL_HOSTS:
        route.continue_()
    else:
        route.abort()


@contextmanager
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=os.environ.get("PROOFHEAL_HEADED") != "1")
        try:
            yield b
        finally:
            b.close()


def new_context(b: Browser) -> BrowserContext:
    """Fresh, isolated context (own cookies/cart). Browser traffic is limited to the local shop."""
    ctx = b.new_context(viewport={"width": 1100, "height": 720})
    ctx.route("**/*", _local_only)
    return ctx
