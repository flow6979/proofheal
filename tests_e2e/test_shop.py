"""End-to-end tests for the demo shop, written against the original (v1) UI."""

from proofheal.harness import Steps


def test_catalog_add_to_cart(ph: Steps) -> None:
    ph.goto("open catalog", "/")
    ph.expect_text("catalog heading shown", "#catalog-title", "Tiny Shop Catalog")
    ph.click("add mug to cart", "#add-mug")
    ph.expect_text("cart count is 1", "#cart-count", "1")


def test_checkout(ph: Steps) -> None:
    ph.goto("open catalog", "/")
    ph.click("add mug to cart", "#add-mug")
    ph.click("add tee to cart", "#add-tee")
    ph.expect_text("cart count is 2", "#cart-count", "2")
    ph.click("go to checkout", "#go-checkout")
    ph.expect_text("order total is $59.98", "#order-total", "$59.98")
    ph.click("pay", "#pay-btn")
    ph.expect_text("confirmation shown", "#confirmation-title", "Thank you for your order!")
    ph.expect_text("charged $59.98", "#confirmation-total", "$59.98")
