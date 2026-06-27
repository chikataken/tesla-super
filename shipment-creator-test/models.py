"""Typed data passed between stages."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawRow:
    """One parsed spreadsheet row (a single VIN), with mapped + cleaned fields."""
    row_number: int                       # 1-based row in the sheet (for error reports)
    vin: str
    fields: dict                          # all canonical fields -> cleaned values
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def get(self, key, default=None):
        return self.fields.get(key, default)


@dataclass
class Vehicle:
    vin: str
    year: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None


@dataclass
class ShipmentDraft:
    """One shipment = one pickup→delivery move carrying 1+ vehicles."""
    group_key: str
    number: str = ""                                # the SD order number to use
    vehicles: list[Vehicle] = field(default_factory=list)
    pickup: dict = field(default_factory=dict)      # name/address/city/state/zip/contact/phone/date
    delivery: dict = field(default_factory=dict)
    price: Optional[float] = None
    notes: str = ""
    source_rows: list[int] = field(default_factory=list)   # sheet rows that fed this


@dataclass
class ParseReport:
    """What the parser understood and what it couldn't."""
    column_mapping: dict = field(default_factory=dict)     # canonical -> source header
    unmapped_headers: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    bad_rows: list[RawRow] = field(default_factory=list)   # rows with validation errors
    total_rows: int = 0
    good_rows: int = 0
