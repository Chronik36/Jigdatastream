from jigstream.extract import _recipient, normalize
from jigstream.mail import parse_message


def raw(headers, body, html=False):
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    ctype = "text/html" if html else "text/plain"
    return (head + f"Content-Type: {ctype}; charset=utf-8\r\n\r\n" + body).encode()


def test_outlook_forward_uses_the_store_sender_subject_and_to():
    body = ("<div><b>From:</b> Pok&eacute;mon Center &lt;orders@pokemoncenter.com&gt;<br>"
            "<b>Sent:</b> Monday, October 20, 2026 9:01 AM<br>"
            "<b>To:</b> jig.profile7@outlook.com &lt;jig.profile7@outlook.com&gt;<br>"
            "<b>Subject:</b> Your order has been canceled</div><div>Order #P0012345 was canceled.</div>")
    e = parse_message(raw({"From": "Jacob <jig.profile7@outlook.com>", "To": "jacob@gmail.com",
                           "Subject": "FW: Your order has been canceled", "Message-ID": "<f1@x>",
                           "Date": "Thu, 23 Oct 2026 10:00:00 +0000"}, body, html=True))
    e.account = "jacob@gmail.com"
    assert "orders@pokemoncenter.com" in e.sender
    assert e.subject == "Your order has been canceled"
    assert e.date.startswith("2026-10-20T09:01")
    assert _recipient(e) == "jig.profile7@outlook.com"
    r = normalize({"kind": "order", "shipping_status": "ordered"}, e)
    assert r["shipping_status"] == "cancelled" and r["retailer"] == "Pokémon Center"


def test_gmail_style_forward_block():
    body = ("---------- Forwarded message ---------\nFrom: Target <orders@target.com>\n"
            "Date: Mon, Oct 20, 2026 at 9:01 AM\nSubject: Thanks for your order\nTo: <buyer@outlook.com>\n\n"
            "Order #9021 total $50.00")
    e = parse_message(raw({"From": "buyer@outlook.com", "To": "jacob@gmail.com", "Subject": "Fwd: Thanks for your order",
                           "Message-ID": "<f2@x>"}, body))
    e.account = "jacob@gmail.com"
    assert e.sender == "Target <orders@target.com>" and _recipient(e) == "buyer@outlook.com"


def test_hide_my_email_keeps_the_hidden_address():
    e = parse_message(raw({"From": "Target <orders@target.com>", "To": "quiet_fox_12@icloud.com",
                           "Delivered-To": "jacob@gmail.com", "Subject": "Thanks for your order",
                           "Message-ID": "<f3@x>"}, "Order #9021"))
    e.account = "jacob@gmail.com"
    assert e.sender == "Target <orders@target.com>" and _recipient(e) == "quiet_fox_12@icloud.com"


def test_plain_mail_mentioning_from_is_not_treated_as_a_forward():
    e = parse_message(raw({"From": "Target <orders@target.com>", "To": "a@x.com", "Subject": "Your receipt",
                           "Message-ID": "<f4@x>"}, "Ships from: Minneapolis\nTotal $5.00"))
    assert e.sender == "Target <orders@target.com>" and e.subject == "Your receipt"
