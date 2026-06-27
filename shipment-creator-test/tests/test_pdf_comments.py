"""Unit tests for splitting the BOL 'Shipment Comments' paragraph into pickup vs
delivery notes (pure; no PDF needed). The fixture mimics pdfplumber's extract_text,
where BOLD labels render with DOUBLED characters and regular text is clean."""
import pdf_read

# the screenshot's BOL, as extract_text would yield it (bold labels doubled)
TWO = ("OOrriiggiinn:: NA-US-IN-Indianapolis DDeessttiinnaattiioonn:: NA-US-MO-St. Louis "
       "CCaarrrriieerr:: TFI Trans Inc "
       "SShhiippmmeenntt CCoommmmeennttss:: "
       "NA-US-IN-Indianapolis: Please drop all vehicles across the street in our off "
       "site parking. Located North East of Fire Stone. Overnight drops okay. Please "
       "lock doors and drop keys in dropbox by front door. DO NOT PARK in first two "
       "rows in off lot., NA-US-MO-St. Louis: Please drop keys in dropbox. "
       "SShhiippmmeenntt IIdd:: 43340317")


def test_splits_pickup_and_delivery_notes():
    n = pdf_read.split_comments(TWO, "NA-US-IN-Indianapolis", "NA-US-MO-St. Louis")
    assert n["pickup"] == ("Please drop all vehicles across the street in our off site "
                           "parking. Located North East of Fire Stone. Overnight drops okay. "
                           "Please lock doors and drop keys in dropbox by front door. DO NOT "
                           "PARK in first two rows in off lot.")
    assert n["delivery"] == "Please drop keys in dropbox."   # trailing 'Shipment Id' trimmed


def test_only_origin_notes_present():
    one = ("SShhiippmmeenntt CCoommmmeennttss:: NA-US-IN-Indianapolis: Lock the gate. "
           "SShhiippmmeenntt IIdd:: 1")
    n = pdf_read.split_comments(one, "NA-US-IN-Indianapolis", "NA-US-MO-St. Louis")
    assert n["pickup"] == "Lock the gate."
    assert n["delivery"] == ""


def test_only_destination_notes_present():
    one = ("SShhiippmmeenntt CCoommmmeennttss:: NA-US-MO-St. Louis: Call on arrival. "
           "SShhiippmmeenntt IIdd:: 2")
    n = pdf_read.split_comments(one, "NA-US-IN-Indianapolis", "NA-US-MO-St. Louis")
    assert n["pickup"] == ""
    assert n["delivery"] == "Call on arrival."


def test_no_comments_yields_empty():
    n = pdf_read.split_comments("SShhiippmmeenntt IIdd:: 3", "A", "B")
    assert n == {"pickup": "", "delivery": ""}


def test_scrubs_send_bol_email_routing():
    # the real-world leak: one or more "send BOL to <emails>" segments must vanish
    raw = ("send BOL to KEthridge@tesla.com,Didi@tfitrans.com,dispatch@tfitrans.com,"
           "info@tfitrans.com,, send BOL to KEthridge@tesla.com,Didi@tfitrans.com,"
           "dispatch@tfitrans.com,info@tfitrans.com")
    assert pdf_read._clean_note(raw) == ""
    assert "@" not in pdf_read._clean_note(raw)


def test_scrub_keeps_surrounding_notes_intact():
    # real instructions before and after the routing segment survive
    raw = ("Drop keys in the lockbox. send BOL to a@tesla.com,b@tfitrans.com "
           "Call dispatch on arrival.")
    out = pdf_read._clean_note(raw)
    assert "Drop keys in the lockbox." in out
    assert "Call dispatch on arrival." in out
    assert "@" not in out


def test_scrub_via_split_comments():
    one = ("SShhiippmmeenntt CCoommmmeennttss:: NA-US-IN-Indianapolis: Lock the gate. "
           "send BOL to dispatch@tfitrans.com,info@tfitrans.com SShhiippmmeenntt IIdd:: 1")
    n = pdf_read.split_comments(one, "NA-US-IN-Indianapolis", "NA-US-MO-St. Louis")
    assert n["pickup"] == "Lock the gate."
    assert "@" not in n["pickup"]


def test_venue_names_in_header_are_not_mistaken_for_comments():
    # the names appear in the header WITHOUT a trailing colon, so they aren't matched
    hdr = "OOrriiggiinn:: NA-US-IN-Indianapolis DDeessttiinnaattiioonn:: NA-US-MO-St. Louis"
    n = pdf_read.split_comments(hdr, "NA-US-IN-Indianapolis", "NA-US-MO-St. Louis")
    assert n == {"pickup": "", "delivery": ""}
