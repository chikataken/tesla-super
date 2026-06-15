"""
All CSS/text/role selectors live here so the rest of the code stays stable
when the portals change their markup.

IMPORTANT — CALIBRATE BEFORE FIRST REAL RUN:
These were derived from the rendered UI, not the live DOM. Run
    playwright codegen https://shipper.superdispatch.com
    playwright codegen https://suppliers.teslamotors.com/logistics/invoicing/regular-fleet
click the elements you care about, and confirm/replace the locators below.
Anything marked # VERIFY is a best guess.
"""

# ----------------------- SuperDispatch -----------------------
# Order list: each order is a card. We read the order-detail link from each card.
SD_ORDER_CARD = "[data-testid='order-list-item'], .order-list-item"      # VERIFY
SD_ORDER_LINK = "a[href*='/orders/view/']"                                # VERIFY
SD_ORDER_TAGS = ".order-tags, [data-testid='order-tags']"                 # VERIFY

# The "Created" date-window dropdown touching the RIGHT of the search box
# (verified via live DOM 2026-06). It's a MUI listbox button that DEFAULTS to
# "1 year ago"; options are 1 month / 3 months / 6 months / 1 year ago / All time.
# Orders older than the selected window don't show in a search, so select_all_time()
# switches it to "All time". select_all_time finds the trigger by its current text
# (a relative-window phrase) and clicks the option by role, so these are reference
# only — not used directly.
SD_TIMEFRAME_TRIGGER = "[role='button'][aria-haspopup='listbox']"  # filtered by window text in code
SD_TIMEFRAME_ALL_TIME = "[role='option']:has-text('All time')"

# Order detail: vehicles table rows hold VIN + delivery date.
SD_VEHICLE_ROW = "table tbody tr"                                          # VERIFY
SD_DETAIL_DELIVERY_BLOCK = "text=Delivery"                                 # VERIFY

# Edit order: Tags multiselect.
SD_EDIT_TAGS_INPUT = "input[role='combobox']"                              # VERIFY (Tags field)
SD_SAVE_BUTTON = "button:has-text('Save')"

# "..." kebab menu on the order detail and its "View Online BOL" item.
SD_KEBAB_BUTTON = "button[aria-haspopup='menu'], button:has-text('•••')"  # VERIFY
SD_VIEW_ONLINE_BOL = "text=View Online BOL"

# ----------------------- Online BOL (bol.superdispatch.com) -----------------------
# The "Delivery Inspection" photo grid. We collect <img> srcs under it.
BOL_DELIVERY_SECTION = "text=Delivery Inspection"                          # VERIFY
BOL_PHOTO_IMG = "img"                                                      # scoped under the delivery section

# ----------------------- Tesla: Regular Fleet > Approved -----------------------
TESLA_APPROVED_TAB = "text=Approved"
TESLA_FULL_VIN_INPUT = "input >> nth=0"            # VERIFY — field under the "Full Vin" label
TESLA_APPLY_BUTTON = "button:has-text('APPLY')"
TESLA_APPROVED_ROW = "table tbody tr"              # VERIFY — result rows
# Within a row, the status cell text (e.g. "Sent for payment", "Paid").
TESLA_APPROVED_STATUS_CELL = "td"                  # VERIFY — pick the right column index in code

# ----------------------- Tesla: Claims > Filed -----------------------
TESLA_CLAIMS_FILED_CARD = "text=Filed"             # the dashboard card that opens the Filed list
TESLA_CLAIM_STATUS_DROPDOWN = "text=Claim Status"  # VERIFY
TESLA_CLAIM_STATUS_OPTION = "[role='option'] input[type='checkbox'], li input[type='checkbox']"  # VERIFY
TESLA_ORIGIN_DEST_DROPDOWN = "text=Origin/Destination Damage"  # VERIFY
TESLA_ORIGIN_DEST_DESTINATION = "text=Destination"
TESLA_CLAIMS_VIN_INPUT = "input[placeholder='Enter VIN']"
TESLA_CLAIMS_SEARCH_BUTTON = "button:has-text('Search')"
# Their UI literally renders "Total Recods: N" (sic). Match both spellings.
TESLA_TOTAL_RECORDS = "text=/Total Reco[rd]+s:/"
