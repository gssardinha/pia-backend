import os
import json
import hashlib
from datetime import datetime
from flask import Flask, request, jsonify, abort
import stripe

# -----------------------------
# CONFIG
# -----------------------------
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_bzYlQgxp3mPYt5HVfZclSzMokw7b6d57")

LICENSES_FILE = "licenses.json"

stripe.api_key = STRIPE_API_KEY

app = Flask(__name__)


# -----------------------------
# LICENSE STORAGE HELPERS
# -----------------------------
def load_licenses():
    if not os.path.exists(LICENSES_FILE):
        return {}
    with open(LICENSES_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_licenses(data):
    with open(LICENSES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def generate_license_key(customer_email: str) -> str:
    base = f"{customer_email}-{datetime.utcnow().isoformat()}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest().upper()
    part1 = digest[0:4]
    part2 = digest[4:8]
    part3 = digest[8:12]
    return f"PIA-USER-{part1}-{part2}-{part3}"


def activate_license_for_subscription(customer_email, customer_id, subscription_id):
    licenses = load_licenses()

    existing_key = None
    for key, info in licenses.items():
        if info.get("stripe_customer") == customer_id:
            existing_key = key
            break

    if existing_key:
        license_key = existing_key
    else:
        license_key = generate_license_key(customer_email)

    licenses[license_key] = {
        "email": customer_email,
        "status": "active",
        "stripe_customer": customer_id,
        "stripe_subscription": subscription_id,
        "created": datetime.utcnow().isoformat(),
        "updated": datetime.utcnow().isoformat(),
    }

    save_licenses(licenses)
    return license_key


def set_license_status_by_subscription(subscription_id, new_status):
    licenses = load_licenses()
    changed = False

    for key, info in licenses.items():
        if info.get("stripe_subscription") == subscription_id:
            info["status"] = new_status
            info["updated"] = datetime.utcnow().isoformat()
            changed = True

    if changed:
        save_licenses(licenses)


# -----------------------------
# STRIPE WEBHOOK
# -----------------------------
@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    print("RAW PAYLOAD:", payload.decode("utf-8", errors="ignore"))

    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        abort(400)
    except stripe.error.SignatureVerificationError:
        abort(400)

    print("EVENT TYPE:", event["type"])
    event_type = event["type"]
    data_object = event["data"]["object"]

    # 1) checkout.session.completed
    if event_type == "checkout.session.completed":
        customer_id = data_object.get("customer")
        subscription_id = data_object.get("subscription")
        customer_email = data_object.get("customer_details", {}).get("email")

        if customer_id and subscription_id and customer_email:
            activate_license_for_subscription(
                customer_email=customer_email,
                customer_id=customer_id,
                subscription_id=subscription_id,
            )

    # 2) invoice.payment_succeeded (old Stripe name)
    elif event_type == "invoice.payment_succeeded":
        subscription_id = data_object.get("subscription")
        if subscription_id:
            set_license_status_by_subscription(subscription_id, "active")

    # 2b) invoice_payment.paid (new Stripe name)
    elif event_type == "invoice_payment.paid":
        subscription_id = data_object.get("subscription")
        if subscription_id:
            set_license_status_by_subscription(subscription_id, "active")

    # 3) invoice.payment_failed
    elif event_type == "invoice.payment_failed":
        subscription_id = data_object.get("subscription")
        if subscription_id:
            set_license_status_by_subscription(subscription_id, "past_due")

    # 4) customer.subscription.created
    elif event_type == "customer.subscription.created":
        subscription = data_object
        subscription_id = subscription.get("id")
        customer_id = subscription.get("customer")

        if customer_id:
            customer = stripe.Customer.retrieve(customer_id)
            customer_email = customer.get("email")
            if customer_email and subscription_id:
                activate_license_for_subscription(
                    customer_email=customer_email,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                )

    # 5) customer.subscription.updated
    elif event_type == "customer.subscription.updated":
        subscription = data_object
        subscription_id = subscription.get("id")
        status = subscription.get("status")

        if subscription_id and status:
            if status in ["active", "trialing"]:
                set_license_status_by_subscription(subscription_id, "active")
            elif status in ["past_due", "unpaid"]:
                set_license_status_by_subscription(subscription_id, "past_due")
            elif status in ["canceled", "incomplete_expired"]:
                set_license_status_by_subscription(subscription_id, "canceled")

    # 6) customer.subscription.deleted
    elif event_type == "customer.subscription.deleted":
        subscription = data_object
        subscription_id = subscription.get("id")
        if subscription_id:
            set_license_status_by_subscription(subscription_id, "canceled")

    return jsonify({"received": True})


# -----------------------------
# LICENSE VALIDATION ENDPOINT
# -----------------------------
@app.route("/validate", methods=["GET"])
def validate_license():
    license_key = request.args.get("key", "").strip()

    if not license_key:
        return jsonify({"status": "invalid", "reason": "missing_key"}), 400

    licenses = load_licenses()
    info = licenses.get(license_key)

    if not info:
        return jsonify({"status": "invalid"}), 200

    status = info.get("status", "invalid")
    return jsonify({"status": status}), 200


# -----------------------------
# HEALTH CHECK
# -----------------------------
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "PIA backend"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))