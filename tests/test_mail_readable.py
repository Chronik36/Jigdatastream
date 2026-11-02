from jigstream.mail import html_to_text, readable


def test_email_viewer_drops_table_bars():
    html = ("<table><tr><td><table><tr><td></td><td>Order placed</td><td></td></tr></table></td></tr>"
            "<tr><td>Fake Comets Blister</td><td>Qty 2</td><td>$27.98</td></tr>"
            "<tr><td> </td><td></td></tr><tr><td>Ship to: Sam Example</td></tr></table>")
    text = html_to_text(html)
    assert " | " in text  # the AI still gets the columns
    shown = readable(text)
    assert "|" not in shown
    assert "Order placed" in shown and "Fake Comets Blister   Qty 2   $27.98" in shown and "Ship to: Sam Example" in shown
    assert readable("A | B\n|  |  |\n\n\nC") == "A   B\n\nC"
