"""Stripe Checkout and PayPal Orders v2, called over plain HTTPS.

Neither gateway's card or account details ever reach this server: buyers pay on the provider's
own page, and we confirm the amount with the provider before issuing tickets.
"""
import hashlib
import hmac
import time
from dataclasses import dataclass

import httpx

from . import config

TIMEOUT = 20


class PaymentError(Exception):
    pass


@dataclass
class Confirmation:
    paid: bool
    amount_cents: int = 0
    currency: str = ""
    payment_ref: str = ""  # Stripe payment intent, or PayPal capture id
    reference: str = ""  # our order id as the provider echoes it back


def enabled() -> list[str]:
    out = []
    if config.STRIPE_SECRET_KEY:
        out.append("stripe")
    if config.PAYPAL_CLIENT_ID and config.PAYPAL_CLIENT_SECRET:
        out.append("paypal")
    return out


NAMES = {"stripe": "Card (Stripe)", "paypal": "PayPal"}


def _cents_to_str(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _str_to_cents(value: str) -> int:
    whole, _, frac = str(value).partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


# ---- Stripe -------------------------------------------------------------------

STRIPE_API = "https://api.stripe.com/v1"


def _stripe(method: str, path: str, data: dict | None = None, idempotency_key: str = "") -> dict:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    try:
        r = httpx.request(method, STRIPE_API + path, data=data, auth=(config.STRIPE_SECRET_KEY, ""),
                          headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise PaymentError("Couldn't reach Stripe. Try again in a minute.") from e
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        msg = body.get("error", {}).get("message", "Stripe rejected the request.")
        raise PaymentError(msg)
    return body


def stripe_checkout(order_id: int, lines: list[tuple[str, int, int]], email: str, success_url: str,
                    cancel_url: str, expires_at: int) -> tuple[str, str]:
    """Create a Checkout Session. lines are (name, unit_cents, qty). Returns (session id, url)."""
    data = {
        "mode": "payment",
        "client_reference_id": str(order_id),
        "metadata[order_id]": str(order_id),
        "payment_intent_data[metadata][order_id]": str(order_id),
        "success_url": success_url,
        "cancel_url": cancel_url,
        "expires_at": str(expires_at),
    }
    if email:
        data["customer_email"] = email
    for i, (name, cents, qty) in enumerate(lines):
        data[f"line_items[{i}][quantity]"] = str(qty)
        data[f"line_items[{i}][price_data][currency]"] = config.CURRENCY.lower()
        data[f"line_items[{i}][price_data][unit_amount]"] = str(cents)
        data[f"line_items[{i}][price_data][product_data][name]"] = name
    sess = _stripe("POST", "/checkout/sessions", data)
    return sess["id"], sess["url"]


def _stripe_confirmation(sess: dict) -> Confirmation:
    return Confirmation(
        paid=sess.get("payment_status") == "paid",
        amount_cents=int(sess.get("amount_total") or 0),
        currency=str(sess.get("currency") or "").upper(),
        payment_ref=str(sess.get("payment_intent") or ""),
        reference=str(sess.get("client_reference_id") or ""),
    )


def stripe_session(session_id: str) -> Confirmation:
    return _stripe_confirmation(_stripe("GET", f"/checkout/sessions/{session_id}"))


def stripe_expire(session_id: str):
    try:
        _stripe("POST", f"/checkout/sessions/{session_id}/expire")
    except PaymentError:
        pass  # already paid, expired or completed; the webhook handles the rest


def stripe_refund(payment_intent: str, order_id: int):
    _stripe("POST", "/refunds", {"payment_intent": payment_intent}, idempotency_key=f"refund-{payment_intent}")


def stripe_verify_webhook(payload: bytes, header: str, tolerance: int = 300) -> bool:
    """Check the Stripe-Signature header: HMAC-SHA256 of "timestamp.payload" with the endpoint secret."""
    secret = config.STRIPE_WEBHOOK_SECRET
    if not secret or not header:
        return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    ts = next((v for k, v in parts if k == "t"), "")
    sigs = [v for k, v in parts if k == "v1"]
    if not ts.isdigit() or not sigs or abs(time.time() - int(ts)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def stripe_event_confirmation(event: dict) -> Confirmation | None:
    if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return None
    return _stripe_confirmation(event.get("data", {}).get("object", {}))


# ---- PayPal -------------------------------------------------------------------

def _paypal_base() -> str:
    return "https://api-m.paypal.com" if config.PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"


def _paypal_token() -> str:
    try:
        r = httpx.post(_paypal_base() + "/v1/oauth2/token", data={"grant_type": "client_credentials"},
                       auth=(config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET), timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise PaymentError("Couldn't reach PayPal. Try again in a minute.") from e
    if r.status_code != 200:
        raise PaymentError("PayPal rejected this site's credentials. Tell an organizer.")
    return r.json()["access_token"]


def _paypal(method: str, path: str, json: dict | None = None, request_id: str = "") -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {_paypal_token()}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    try:
        r = httpx.request(method, _paypal_base() + path, json=json, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise PaymentError("Couldn't reach PayPal. Try again in a minute.") from e
    return r.status_code, (r.json() if r.content else {})


def paypal_create(order_id: int, total_cents: int, description: str, return_url: str, cancel_url: str) -> tuple[str, str]:
    """Create a PayPal order. Returns (PayPal order id, approval url)."""
    status, body = _paypal("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": str(order_id),
            "custom_id": str(order_id),
            "description": description[:127],
            "amount": {"currency_code": config.CURRENCY, "value": _cents_to_str(total_cents)},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": config.SITE_NAME[:127], "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING", "return_url": return_url, "cancel_url": cancel_url,
        }}},
    })
    if status >= 400:
        raise PaymentError(body.get("message", "PayPal rejected the order."))
    link = next((l["href"] for l in body.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
    if not link:
        raise PaymentError("PayPal didn't return a payment page.")
    return body["id"], link


def _paypal_confirmation(body: dict) -> Confirmation:
    unit = (body.get("purchase_units") or [{}])[0]
    cap = ((unit.get("payments") or {}).get("captures") or [{}])[0]
    amount = cap.get("amount") or {}
    return Confirmation(
        paid=body.get("status") == "COMPLETED" and cap.get("status") == "COMPLETED",
        amount_cents=_str_to_cents(amount.get("value", "0")),
        currency=str(amount.get("currency_code", "")).upper(),
        payment_ref=str(cap.get("id", "")),
        reference=str(unit.get("reference_id") or cap.get("custom_id") or ""),
    )


def paypal_capture(paypal_order_id: str, order_id: int) -> Confirmation:
    status, body = _paypal("POST", f"/v2/checkout/orders/{paypal_order_id}/capture", {},
                           request_id=f"capture-{paypal_order_id}")
    if status >= 400:
        # Already captured (e.g. a double click): read the order back instead.
        status, body = _paypal("GET", f"/v2/checkout/orders/{paypal_order_id}")
        if status >= 400:
            raise PaymentError("PayPal couldn't complete the payment. You haven't been charged.")
    return _paypal_confirmation(body)


def paypal_refund(capture_id: str, order_id: int):
    status, body = _paypal("POST", f"/v2/payments/captures/{capture_id}/refund", {},
                           request_id=f"refund-{capture_id}")
    if status >= 400:
        raise PaymentError(body.get("message", "PayPal rejected the refund."))
