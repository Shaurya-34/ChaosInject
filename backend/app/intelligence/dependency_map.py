"""Business operation -> workload dependencies for the vendored Online Boutique.

Derived by reading the vendored upstream source in this repository, not guessed:

* ``application/online-boutique/src/frontend/main.go`` routes:
  ``GET /`` -> homeHandler, ``GET /product/{id}`` -> productHandler,
  ``POST /cart`` -> addToCartHandler, ``GET /cart`` -> viewCartHandler,
  ``POST /cart/checkout`` -> placeOrderHandler.
* ``frontend/handlers.go``: a failed currency, product, cart or shipping call
  renders HTTP 500 (hard). ``chooseAd`` and ``getRecommendations`` failures are
  only logged and the page still renders (soft).
* ``checkoutservice/main.go``: failure to read the cart/products/currency/
  shipping quote, to charge the card or to ship returns an error (hard);
  ``emptyUserCart`` errors are ignored and a failed confirmation email is only
  logged (soft).
* ``cartservice`` stores carts in ``redis-cart``, so every cart call depends on it.

Operation names follow Navya's workload (``telemetry/workload.py``). ``cart`` is
the single operation used by the shared synthetic fixtures. Re-verify this map
whenever the pinned upstream revision changes.
"""

from __future__ import annotations

_CART = frozenset({"cartservice", "redis-cart"})

HARD_DEPENDENCIES: dict[str, frozenset[str]] = {
    "browse": frozenset({"frontend", "currencyservice", "productcatalogservice"}) | _CART,
    "product_view": frozenset({"frontend", "productcatalogservice", "currencyservice"}) | _CART,
    "add_to_cart": frozenset({"frontend", "productcatalogservice"}) | _CART,
    "cart_read": frozenset({"frontend", "currencyservice", "productcatalogservice", "shippingservice"}) | _CART,
    "checkout": frozenset(
        {"frontend", "checkoutservice", "productcatalogservice", "currencyservice", "shippingservice", "paymentservice"}
    )
    | _CART,
    "cart": frozenset({"frontend"}) | _CART,
}

SOFT_DEPENDENCIES: dict[str, frozenset[str]] = {
    "browse": frozenset({"adservice"}),
    "product_view": frozenset({"adservice", "recommendationservice"}),
    "add_to_cart": frozenset(),
    "cart_read": frozenset({"recommendationservice"}),
    "checkout": frozenset({"emailservice", "recommendationservice"}),
    "cart": frozenset(),
}

FIXTURE_ALIASES = frozenset({"cart"})
BUSINESS_CRITICAL_OPERATIONS = frozenset({"add_to_cart", "cart_read", "checkout", "cart"})


def classify(operation: str, target: str) -> str:
    """Return ``hard``, ``soft``, ``unrelated`` or ``unknown_operation``."""

    if operation not in HARD_DEPENDENCIES:
        return "unknown_operation"
    if target in HARD_DEPENDENCIES[operation]:
        return "hard"
    if target in SOFT_DEPENDENCIES.get(operation, frozenset()):
        return "soft"
    return "unrelated"


def operations_for(target: str) -> tuple[list[str], list[str]]:
    """(hard, soft) workload operations for a target, excluding fixture aliases."""

    ops = [op for op in HARD_DEPENDENCIES if op not in FIXTURE_ALIASES]
    return (
        [op for op in ops if classify(op, target) == "hard"],
        [op for op in ops if classify(op, target) == "soft"],
    )
