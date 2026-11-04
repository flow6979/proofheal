"""Tiny synthetic demo shop used to exercise ProofHeal.

UI variant is chosen with the SHOP_VARIANT env var:
  v1         original UI the e2e tests are written against
  v2         restyled UI: renamed ids/classes, moved elements, same behavior
  v2-broken  v2 visuals, but checkout is genuinely broken
             (Pay button silently no-ops, order total is miscalculated)
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

PRODUCTS = {
    "mug": {"sku": "mug", "name": "Ceramic Mug", "price_cents": 2499},
    "tee": {"sku": "tee", "name": "Cotton Tee", "price_cents": 3499},
    "notebook": {"sku": "notebook", "name": "Dot Notebook", "price_cents": 1250},
}

VARIANTS = {"v1", "v2", "v2-broken"}


def fmt(cents: int) -> str:
    return f"${cents / 100:.2f}"


def order_total_cents(cart: dict[str, int], broken: bool) -> int:
    if broken:
        # Deliberate regression for the demo: prices are truncated to whole dollars.
        return sum((PRODUCTS[s]["price_cents"] // 100) * 100 * q for s, q in cart.items())
    return sum(PRODUCTS[s]["price_cents"] * q for s, q in cart.items())


def create_app(variant: str | None = None) -> Flask:
    variant = variant or os.environ.get("SHOP_VARIANT", "v1")
    if variant not in VARIANTS:
        raise SystemExit(f"Unknown SHOP_VARIANT {variant!r}; expected one of {sorted(VARIANTS)}")
    template_dir = "v1" if variant == "v1" else "v2"
    broken = variant == "v2-broken"

    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates" / template_dir))
    app.secret_key = os.urandom(16)  # per-process, demo only
    orders: dict[int, dict] = {}
    order_ids = itertools.count(1001)

    def cart() -> dict[str, int]:
        return dict(session.get("cart", {}))

    def cart_lines(c: dict[str, int]) -> list[dict]:
        return [
            {**PRODUCTS[s], "qty": q, "line": fmt(PRODUCTS[s]["price_cents"] * q), "price": fmt(PRODUCTS[s]["price_cents"])}
            for s, q in c.items()
        ]

    @app.context_processor
    def inject():
        return {"cart_count": sum(cart().values()), "variant": variant, "broken": broken}

    @app.get("/")
    def catalog():
        products = [{**p, "price": fmt(p["price_cents"])} for p in PRODUCTS.values()]
        return render_template("catalog.html", products=products)

    @app.post("/cart/add")
    def cart_add():
        sku = request.form.get("sku", "")
        if sku not in PRODUCTS:
            abort(400)
        c = cart()
        c[sku] = c.get(sku, 0) + 1
        session["cart"] = c
        return redirect(url_for("catalog"))

    @app.get("/checkout")
    def checkout():
        c = cart()
        return render_template(
            "checkout.html", lines=cart_lines(c), total=fmt(order_total_cents(c, broken)), empty=not c
        )

    @app.post("/api/order")
    def place_order():
        c = cart()
        if not c:
            return jsonify({"error": "cart is empty"}), 400
        oid = next(order_ids)
        orders[oid] = {"lines": cart_lines(c), "total": fmt(order_total_cents(c, broken))}
        session["cart"] = {}
        return jsonify({"order_id": oid})

    @app.get("/confirmation/<int:oid>")
    def confirmation(oid: int):
        if oid not in orders:
            abort(404)
        return render_template("confirmation.html", order=orders[oid], order_id=oid)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "variant": variant}

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5055"))
    create_app().run(host="127.0.0.1", port=port, debug=False)
