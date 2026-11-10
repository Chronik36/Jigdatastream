"""Rule-first field reading (jigstream/rulefields.py).

Every email here is made up. The layouts mirror real store emails (how labels, values, table cells and links are
laid out after html_to_text), but no name, address, number or price comes from a real email.
"""
import pytest

from jigstream import rulefields
from jigstream.extract import cancel_signal, find_order_number, normalize
from jigstream.mail import Email

URL = "[https://click.shop.example/t?r=abc123]"


def mail(subject, body, sender="Shop <orders@shop.example>"):
    return Email(f"<{subject}@x>", sender, subject, "2026-10-01T10:00:00+00:00", body, account="me@gmail.com")


def read(subject, body):
    return rulefields.read(mail(subject, body))


# ---------------------------------------------------------------- layouts

TARGET_STYLE = f"""Get ready! We'll email you when your tracking info is available.
 |
 Order #555000111222333
 |
 | Thanks for your order, Pat!
 Placed November 1, 2026
 | Order total
 | $47.35
 | {URL} Visit order details
 Shipping
 |
 Delivers to:
 | Pat Example, 12 Sample Road, Testville, TX 75001-1234
 | Booster Bundle
 Qty: 1
 $39.99 / ea
 Arrives Wed, Nov 4
 Order Summary
 Subtotal (1 item)
 | $39.99
 Delivery
 Shipping
 | $4.99
 Estimated taxes
 Based on 75001
 | $2.37
 Total
 | $47.35
"""


def test_target_style_confirmation():
    got = read("Thanks for shopping with us! Here's your order #:555000111222333.", TARGET_STYLE)
    assert got["order_number"] == "555000111222333"
    assert (got["total"], got["subtotal"], got["shipping"], got["tax"]) == (47.35, 39.99, 4.99, 2.37)
    assert got["sum_ok"]
    assert (got["name"], got["street"], got["city"], got["state"], got["zip"]) == \
        ("Pat Example", "12 Sample Road", "Testville", "TX", "75001")
    assert got["cancel"] is None


def test_order_number_glued_to_the_next_word():
    got = read("Items from order #555000999888777are about to ship.",
               "Delivers to: Sam Sample, 7 Elm Way, Exampleton, OH, 44001\nFEDEX GROUND Tracking # 123")
    assert got["order_number"] == "555000999888777"
    assert (got["name"], got["street"], got["zip"]) == ("Sam Sample", "7 Elm Way", "44001")


SHOPIFY_STYLE = """Order #48213
Thank you for your order!
We're getting your order ready to be shipped. We will notify you when it has been sent.
 | View your order
Order summary
Mystery Pack × 2
$60.00
Subtotal
$60.00
Shipping
$8.00
State Sales Tax
$2.40
County Sales Tax
$2.40
Total
$72.80 USD
Customer information
Shipping address
Robin Sample
44 Placeholder Ave APT 3B
Sampleville NY 10001
United States
Billing address
Robin Sample
"""


def test_shopify_style_summary_and_address():
    got = read("Order #48213 confirmed", SHOPIFY_STYLE)
    assert got["order_number"] == "48213"
    assert (got["subtotal"], got["shipping"], got["tax"], got["total"]) == (60.0, 8.0, 4.8, 72.8)  # two tax lines
    assert got["sum_ok"]
    assert (got["name"], got["street"], got["city"], got["state"], got["zip"]) == \
        ("Robin Sample", "44 Placeholder Ave APT 3B", "Sampleville", "NY", "10001")


def test_store_prefixed_order_number_and_whole_dollars():
    body = SHOPIFY_STYLE.replace("Order #48213", "Order ABC#77120").replace(".00", "").replace("$2.40", "$2")
    got = read("Order ABC#77120 confirmed", body)
    assert got["order_number"] == "ABC#77120"
    assert got["total"] == 72.80 and got["subtotal"] == 60


def test_full_state_name_in_city_line():
    got = read("Order #48213 confirmed", SHOPIFY_STYLE.replace("Sampleville NY 10001", "Sampleville, New York 10001"))
    assert (got["city"], got["state"], got["zip"]) == ("Sampleville", "New York", "10001")


COLUMN_STYLE = """Order Number: W100200300
Ship to Home (2 items)
$18.00
Shipping Address:
Lee Example
9 Test Court
Demo City, PA 19001
Subtotal
Shipping
Tax
$18.00
$5.95
$1.44
Order Total
$25.39
"""


def test_labels_in_one_column_amounts_in_the_next():
    got = read("yay, your order W100200300 is confirmed!", COLUMN_STYLE)
    assert got["order_number"] == "W100200300"
    assert (got["subtotal"], got["shipping"], got["tax"], got["total"]) == (18.0, 5.95, 1.44, 25.39)
    assert (got["name"], got["street"], got["zip"]) == ("Lee Example", "9 Test Court", "19001")  # not "Home (2 items)"


def test_label_and_amount_on_one_line_and_short_cents():
    body = "Order Summary\nSubtotal (1 items) | $19.96\nTAX 1 | $1.6\nTotal paid | $21.56\nVisa, $21.56"
    got = read("Here's your receipt", body)
    assert (got["subtotal"], got["tax"], got["total"]) == (19.96, 1.6, 21.56)
    assert got["sum_ok"]


WALMART_STYLE = f"""Order date: Mon, Oct 5, 2026
 | Order number: {URL}
#7000123-45678901
 | Thanks for your order, Kim!
 | What to expect
 | Delivery
 | Arrives Mon, Oct 12
 |
 | 88 Fictional Blvd, Apt 2, Mocktown, NY, 12000, USA
 | 1 item
 | Order total
 | Includes all fees, taxes and discounts
 | $64.12
"""


def test_walmart_style_number_after_link_and_unlabelled_address():
    got = read("Thanks for your delivery order, Kim", WALMART_STYLE)
    assert got["order_number"] == "7000123-45678901"
    assert got["total"] == 64.12
    assert (got["street"], got["city"], got["state"], got["zip"]) == ("88 Fictional Blvd, Apt 2", "Mocktown", "NY", "12000")


def test_amazon_style_order_number_behind_direction_mark():
    body = "Jo, thanks for your order!\nArriving Friday\nOrder # ‫111-2223334-5556667\nGrand Total: | $21.48"
    got = read("Ordered 2 items: Toys", body)
    assert got["order_number"] == "111-2223334-5556667"
    assert got["total"] == 21.48


def test_order_number_after_a_word_that_follows_order():
    body = ("We're sorry that the below item(s) from your order couldn't be processed hence been canceled.\n"
            "Order Details\nOrder Number: Q0099887766\nDate Ordered: July 1, 2026")
    assert read("Your order has been canceled", body)["order_number"] == "Q0099887766"
    assert find_order_number(mail("Your order has been canceled", body)) == "Q0099887766"


def test_last_digits_are_not_the_order_number():
    got = read("You've canceled an item from your order ending in 4321.", "Order #555000111224321\nCanceled item")
    assert got["order_number"] == "555000111224321"


def test_store_footer_address_is_not_the_ship_to():
    body = ("Thanks for your order!\nOrder # 111-2223334-5556667\nTotal | $5.00\n"
            "Unsubscribe | Privacy Notice\n100 Corporate Way, Bigcity, WA, 98000")
    got = read("Ordered 1 item", body)
    assert got["street"] is None and got["zip"] is None


# ---------------------------------------------------------------- cancels

def test_item_cancel_is_items_and_still_cancels_a_one_item_order():
    body = "Order #555000111224321\nYour item has been canceled\nAs you requested, we have canceled the item below."
    e = mail("You've successfully canceled an item from your order ending in 4321.", body)
    assert rulefields.read(e)["cancel"] == "items"
    assert cancel_signal(e) == "cancelled"


def test_item_cancel_with_rest_of_order_coming_is_not_a_cancelled_order():
    body = "We canceled one item from your order. The rest of your order will still ship as planned."
    e = mail("An item in your order was canceled", body)
    assert rulefields.read(e)["cancel"] == "items"
    assert cancel_signal(e) is None


def test_items_cancelled_subject_but_whole_order_body():
    body = "Your items were cancelled\nYour order was cancelled. You have not been charged for this order."
    assert read('Items cancelled successfully: 2 "Sample Card Game..."', body)["cancel"] == "full"


@pytest.mark.parametrize("subject", ["Canceled: delivery of Sample Box +1 item", "Your order has been canceled",
                                     "You successfully canceled an order"])
def test_full_cancels(subject):
    assert read(subject, "We're sorry, we had to cancel your delivery order.")["cancel"] == "full"


@pytest.mark.parametrize("subject,body", [
    ("Discover the magic of Active Noise Cancellation with Earbuds 3.", "Now starting at $129. Fast, free delivery."),
    ("Order #171000US confirmed", "In the event that we are unable to fulfill your order, any remaining items will be "
                                  "cancelled and refunded.\nOrder summary\nSubtotal\n$10.00\nTotal\n$10.00 USD"),
])
def test_not_cancels(subject, body):
    assert read(subject, body)["cancel"] is None


def test_refund():
    assert read("Advance refund issued for Sample Jacket", "Refund subtotal | $9.99\nTotal refund* | $9.99")["cancel"] \
        == "refund"


# ---------------------------------------------------------------- wired into normalize

def test_normalize_fills_what_the_model_missed():
    got = normalize({"kind": "order", "items": [{"name": "Booster Bundle", "price": 39.99}]},
                    mail("Thanks for shopping with us! Here's your order #:555000111222333.", TARGET_STYLE))
    assert got["order_number"] == "555000111222333"
    assert got["total"] == 47.35
    assert got["shipping_name"] == "Pat Example"
    assert got["shipping_address"] == "12 Sample Road, Testville, TX 75001"


def test_normalize_takes_the_summary_total_over_a_wrong_one():
    got = normalize({"kind": "order", "total": 39.99}, mail("Order #48213 confirmed", SHOPIFY_STYLE))
    assert got["total"] == 72.80


def test_currency_prefix_and_order_no_label():
    body = "Order NO. 1001002003\nItem Total US$ 50.00\nShipping US$ 5.00\nTax US$ 4.40\nOrder Total US$ 59.40"
    got = read("Order Confirmation", body)
    assert got["order_number"] == "1001002003"
    assert (got["subtotal"], got["shipping"], got["tax"], got["total"], got["sum_ok"]) == (50.0, 5.0, 4.4, 59.4, True)
