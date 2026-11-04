"""Plain pytest fixtures: run the e2e suite as written (no recording, no healing).

    SHOP_VARIANT=v1 pytest tests_e2e
"""

import os

import pytest

from proofheal.env import browser, new_context, shop_server
from proofheal.harness import Steps


@pytest.fixture(scope="session")
def shop_url():
    with shop_server(os.environ.get("SHOP_VARIANT", "v1")) as url:
        yield url


@pytest.fixture(scope="session")
def _browser():
    with browser() as b:
        yield b


@pytest.fixture
def ph(_browser, shop_url, request):
    ctx = new_context(_browser)
    page = ctx.new_page()
    try:
        yield Steps(page, shop_url, request.node.name)
    finally:
        ctx.close()
