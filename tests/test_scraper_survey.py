"""Tracking check digits, more cancel wording, JSON schema replies, field grounding and layout matching.
Every order detail here is made up."""
import io
import json
import urllib.error

import pytest

from jigstream import grounding, llm, packs, tracknum
from jigstream.extract import REPLY_SCHEMA, cancel_signal, carrier_for, find_tracking, normalize
from jigstream.mail import Email


def mail(subject, body, sender="Shop <orders@shop.example>"):
    return Email(f"<{subject}@x>", sender, subject, "2026-10-01T10:00:00+00:00", body, account="me@gmail.com")


def with_check_digit(prefix):
    """prefix + the one last digit that makes it a valid tracking number."""
    for d in "0123456789":
        if tracknum.identify(prefix + d)[0]:
            return prefix + d
    raise AssertionError(prefix)


# ---- tracking numbers ----

def test_every_sample_number_in_the_data_set_is_known():
    missed = []
    for fmt in tracknum.formats():
        for number in fmt["tests"].get("valid", []):
            if "   " in number:
                continue  # the data set's triple-spaced samples; emails write numbers with single spaces
            if not tracknum.identify(number)[0] or not tracknum.find_all("Tracking number: " + number):
                missed.append((fmt["id"], number))
        for number in fmt["tests"].get("invalid", []):
            assert tracknum.identify(number)[1] != fmt["id"], (fmt["id"], number)
    assert not missed


def test_new_carriers_and_check_digits():
    ontrac = with_check_digit("C1234567890123")
    laser = "LX12345678"
    assert find_tracking(f"Your package is on the way. OnTrac {ontrac}") == (ontrac, "OnTrac")
    assert find_tracking(f"Shipped with LaserShip: {laser}") == (laser, "LaserShip")
    assert carrier_for(ontrac) == "OnTrac"
    bad = ontrac[:-1] + str((int(ontrac[-1]) + 1) % 10)
    assert tracknum.identify(bad) == (None, None)
    usps = with_check_digit("940011189922310000000")
    spaced = " ".join(usps[i:i + 4] for i in range(0, len(usps), 4))
    assert find_tracking(f"USPS tracking {spaced}") == (usps, "USPS")


def test_order_numbers_and_short_numbers_are_not_tracking():
    fedex = with_check_digit("12345678901")
    assert tracknum.find_all(f"Order #{fedex} placed") == []          # an order number, whatever it fits
    assert tracknum.find_all(f"Thanks! Reference {fedex}") == []      # short, no tracking word near it
    assert tracknum.find_all(f"FedEx tracking: {fedex}") == [(fedex, "FedEx")]
    assert "ontrac.com" in tracknum.url_for(with_check_digit("C1234567890123"))


# ---- cancels ----

@pytest.mark.parametrize("subject,body", [
    ("Update on your order", "We had to cancel your order #A12345 because the item is out of stock."),
    ("About your order", "Unfortunately we are unable to fulfill your order 55501."),
    ("Order update", "Your order has been voided and you were not charged."),
    ("Your order", "We canceled your order W99881."),
    ("Order P0123", "Order P0123 could not be completed and has been cancelled."),
    ("Order 1234 Voided", ""),
    ("Unable to fulfill your order", ""),
    ("Your order", "Your order 88321 was declined and will not ship."),
    ("Order update", "We were unable to complete your order, so it has been cancelled."),
])
def test_more_cancel_wording(subject, body):
    assert cancel_signal(mail(subject, body)) == "cancelled"


@pytest.mark.parametrize("subject,body", [
    ("Order confirmed", "If we have to cancel your order, we'll let you know."),
    ("Action needed", "We may cancel your order if we can't verify it."),
    ("Payment issue", "Your order payment was declined. Please update your card."),
    ("Your subscription", "We had to cancel your subscription."),
    ("Your order shipped", "Need to cancel? Visit our help center."),
])
def test_policy_lines_are_not_cancels(subject, body):
    assert cancel_signal(mail(subject, body)) is None


# ---- JSON schema replies ----

class Reply:
    def __init__(self, content):
        self.content = content

    def body(self):
        return {"choices": [{"message": {"content": self.content}}]}


def test_schema_is_sent_and_engines_without_it_fall_back(monkeypatch):
    sent = []
    client = llm.LLMClient("http://127.0.0.1:9/v1", "some-model")
    monkeypatch.setattr(llm.LLMClient, "no_schema", set())

    def send(req):
        payload = json.loads(req.data)
        sent.append(payload.get("response_format"))
        if payload["response_format"]["type"] == "json_schema":
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"error": "unknown format"}'))
        return Reply('{"kind": "other"}').body()

    monkeypatch.setattr(client, "_send", send)
    assert client.chat_json("sys", "email", schema=REPLY_SCHEMA) == {"kind": "other"}
    assert [f["type"] for f in sent] == ["json_schema", "json_object"]
    assert sent[0]["json_schema"]["schema"]["properties"]["kind"]["enum"][0] == "order"
    client.chat_json("sys", "email", schema=REPLY_SCHEMA)  # remembered: straight to JSON mode
    assert [f["type"] for f in sent] == ["json_schema", "json_object", "json_object"]


# ---- grounding ----

BODY = """Thanks for your order!
Order #ZX-4410
Ship to:
Pat Example
12 Sample Road
Testville, TX 75001
Item: Booster Box  $40.00
Subtotal $40.00
Shipping $5.00
Tax $3.30
Total $48.30
"""


@pytest.mark.grounded
def test_values_the_email_does_not_show_are_dropped():
    e = mail("Thanks for your order", BODY)
    got = normalize({"kind": "order", "order_number": "ZX-9999", "shipping_name": "Someone Else",
                     "shipping_address": "99 Other Street, Elsewhere, CA 90001", "total": 48.30,
                     "tracking_number": "1Z999AA10123456784"}, e)
    assert got["order_number"] == "ZX-4410"          # the model's number wasn't in the email; the email's is
    assert got["shipping_name"] == "Pat Example"     # made-up values give way to the email's own ship-to lines
    assert got["shipping_address"].startswith("12 Sample Road")
    assert got["tracking_number"] == "1Z999AA10123456784"  # not shown, but a real UPS number: kept


@pytest.mark.grounded
def test_values_the_email_shows_stay():
    e = mail("Thanks for your order", BODY)
    got = normalize({"kind": "order", "order_number": "ZX4410", "shipping_name": "Pat Example",
                     "shipping_address": "12 Sample Road, Testville, TX 75001", "total": 48.30}, e)
    assert (got["order_number"], got["shipping_name"], got["total"]) == ("ZX4410", "Pat Example", 48.30)
    assert got["shipping_address"].startswith("12 Sample")


@pytest.mark.grounded
def test_subtotal_taken_for_total_is_fixed():
    e = mail("Thanks for your order", BODY)
    got = normalize({"kind": "order", "order_number": "ZX-4410", "total": 40.00,
                     "items": [{"name": "Booster Box", "quantity": 1, "price": 40.00}]}, e)
    assert got["total"] == 48.30


@pytest.mark.grounded
def test_order_number_given_as_tracking_is_swapped_for_the_real_one():
    ontrac = with_check_digit("C1234567890123")
    e = mail("Your order shipped", f"Order #77881234\nYour package shipped. OnTrac tracking {ontrac}")
    got = normalize({"kind": "order", "order_number": "77881234", "tracking_number": "77881234"}, e)
    assert (got["tracking_number"], got["carrier"]) == (ontrac, "OnTrac")


def test_grounding_is_off_for_other_tests():
    assert grounding.ENABLED is False


# ---- layout memory ----

def test_examples_from_the_same_layout_come_first():
    template = "Order #{n}\nShip to:\n{name}\nSubtotal ${s}\nShipping $5.00\nTax $1.00\nTotal ${t}\nThanks for shopping"
    same = {"sender_domain": "storeone.example", "subject": "Order received",
            "prompt": template.format(n=111, name="A Person", s="10.00", t="16.00"), "answer": '{"kind": "order"}'}
    other = {"sender_domain": "news.example", "subject": "Weekly picks",
             "prompt": "Big sale this week\nNew arrivals\nShop now", "answer": '{"kind": "other"}'}
    pick = packs.Picker([other, same])
    e = mail("Confirmation", template.format(n=222, name="B Person", s="20.00", t="26.00"),
             sender="Store Two <hi@storetwo.example>")  # another store on the same template
    assert pick(e)[0][1] == '{"kind": "order"}'
    assert packs.same_layout(packs.layout(same["prompt"]), packs.layout(e.body)) > 0.5
