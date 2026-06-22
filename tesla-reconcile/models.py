"""Plain data structures passed between the modules."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class OrderRow:
    """A row scraped from the Invoiced list."""
    order_id: str
    detail_url: str
    tags: list[str] = field(default_factory=list)
    flagged: bool = False

    @property
    def should_skip(self) -> bool:
        from config import SKIP_TAGS
        if self.flagged:                       # red "order flagged" marker
            return True
        return any(t.strip().lower() in SKIP_TAGS for t in self.tags)


@dataclass
class Vehicle:
    vin: str
    delivery_date: Optional[date] = None
    delivery_zip: Optional[str] = None


@dataclass
class OrderDetail:
    order_id: str
    detail_url: str
    edit_url: str
    vehicles: list[Vehicle] = field(default_factory=list)
    delivery_zip: Optional[str] = None


@dataclass
class PaymentResult:
    ok: bool
    status: Optional[str] = None      # e.g. "Sent for payment"
    note: str = ""                    # why it failed, if it did
    indeterminate: bool = False       # couldn't read the portal (timeout/error) —
                                      # NOT the same as "no payment"; don't tag SUS


@dataclass
class ClaimResult:
    has_claim: bool                   # a CHARGEABLE destination claim (reported/filed
                                      # on or after delivery) -> drives the "Damage claim" tag
    record_count: int = 0             # number of DESTINATION claim records for the VIN
    indeterminate: bool = False       # couldn't read the portal / no delivery date — don't decide
    pre_existing: bool = False        # destination claim(s) found, but all dated BEFORE
                                      # delivery -> pre-existing damage, NOT chargeable
    note: str = ""                    # human-readable detail for the log (claim #, dates)


@dataclass
class VisionResult:
    """Returned by the Claude vision module for an order's delivery photos."""
    vin_photo_found: bool
    vin_read: Optional[str] = None        # best-effort VIN read off the vehicle
    vin_mismatch: bool = False            # a legible VIN was read but it's the wrong car
    location_ok: Optional[bool] = None    # photo zip matches expected delivery zip
    zip_on_stamp: Optional[str] = None
    confidence: float = 0.0
    reasoning: str = ""                   # short why behind the decision (troubleshoot)
    raw: str = ""                         # raw model text for auditing


@dataclass
class ZipCheckResult:
    """Claude's comparison of the scheduled vs actual delivered ZIP codes."""
    too_far: bool                          # >= threshold driving minutes apart
    drive_minutes: Optional[int] = None    # estimated driving time between the ZIPs
    same_metro: Optional[bool] = None      # same city / metro area
    reasoning: str = ""
    raw: str = ""


@dataclass
class OrderOutcome:
    order_id: str
    decision: str                          # tag applied, or "SKIPPED: ..."
    tags_applied: list[str] = field(default_factory=list)
    needs_review: bool = False
    detail: str = ""
