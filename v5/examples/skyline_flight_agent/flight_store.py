"""In-memory flight catalog + bookings for the SkyLine Airways demo agent.

A self-contained fake backend so the voice agent is genuinely interactive:
you can search a route, book a seat, look the booking up by its PNR, and
cancel it for a refund — all stateful within one process. No real airline
API. (This is the plain agent; plivo-mirror is plugged in later.)
"""

from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass, field
from datetime import datetime

# ── airport name lookup so callers can say "New York" or "JFK" ──
AIRPORTS = {
    "JFK": "New York", "EWR": "New York", "LGA": "New York",
    "LAX": "Los Angeles", "SFO": "San Francisco", "ORD": "Chicago",
    "MIA": "Miami", "BOS": "Boston", "SEA": "Seattle", "DEN": "Denver",
    "ATL": "Atlanta", "DFW": "Dallas", "LAS": "Las Vegas", "AUS": "Austin",
}
_CITY_TO_CODE = {}
for _code, _city in AIRPORTS.items():
    _CITY_TO_CODE.setdefault(_city.lower(), _code)

AIRLINES = [
    ("SkyLine", "SK"), ("BlueJet", "BJ"), ("Northwind", "NW"),
    ("Pacific", "PC"), ("Coastal", "CL"),
]


def resolve_airport(text: str) -> str | None:
    """Map a spoken city or 3-letter code to an airport code."""
    if not text:
        return None
    t = text.strip().upper()
    if t in AIRPORTS:
        return t
    return _CITY_TO_CODE.get(text.strip().lower())


def _seed(*parts: str) -> int:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(h[:8], 16)


def search_flights(origin: str, destination: str, date: str) -> list[dict]:
    """Deterministic pseudo-catalog: the same route+date always returns the
    same options, so a caller can re-ask and hear consistent flights."""
    o = resolve_airport(origin) or (origin or "").upper()[:3]
    d = resolve_airport(destination) or (destination or "").upper()[:3]
    base = _seed(o, d, date)
    flights: list[dict] = []
    for i in range(3):
        airline, code = AIRLINES[(base + i) % len(AIRLINES)]
        num = 100 + (base // (i + 1)) % 800
        dep_h = 6 + (base + i * 7) % 14          # 6:00–19:00
        dur_m = 95 + (base + i * 13) % 240        # 1h35–5h35
        price = 119 + (base // (i + 3)) % 480     # $119–$599
        seats = 2 + (base + i) % 9
        dep = f"{dep_h:02d}:{(base * (i + 1)) % 60:02d}"
        arr_total = dep_h * 60 + (base * (i + 1)) % 60 + dur_m
        arr = f"{(arr_total // 60) % 24:02d}:{arr_total % 60:02d}"
        flights.append({
            "flight_number": f"{code}{num}",
            "airline": airline,
            "origin": o, "destination": d, "date": date,
            "depart": dep, "arrive": arr,
            "duration_min": dur_m,
            "price_usd": price,
            "seats_left": seats,
        })
    flights.sort(key=lambda f: f["price_usd"])
    return flights


@dataclass
class Booking:
    pnr: str
    passenger: str
    flight_number: str
    airline: str
    origin: str
    destination: str
    date: str
    depart: str
    price_usd: int
    age: int | None = None
    phone: str = ""
    cabin: str = "economy"
    status: str = "CONFIRMED"   # CONFIRMED | CANCELLED


@dataclass
class FlightStore:
    bookings: dict[str, Booking] = field(default_factory=dict)

    def _new_pnr(self) -> str:
        alphabet = string.ascii_uppercase + string.digits
        n = _seed(str(len(self.bookings)), datetime.utcnow().isoformat())
        out = ""
        for _ in range(6):
            out += alphabet[n % len(alphabet)]
            n //= len(alphabet)
        return out if out not in self.bookings else self._new_pnr()

    def book(
        self,
        flight: dict,
        passenger: str,
        *,
        age: int | None = None,
        phone: str = "",
        cabin: str = "economy",
    ) -> Booking:
        # business-class is priced at 2.2x the base economy fare
        price = flight["price_usd"]
        if (cabin or "").lower().startswith("business"):
            price = int(round(price * 2.2))
        b = Booking(
            pnr=self._new_pnr(),
            passenger=passenger,
            flight_number=flight["flight_number"],
            airline=flight["airline"],
            origin=flight["origin"],
            destination=flight["destination"],
            date=flight["date"],
            depart=flight["depart"],
            price_usd=price,
            age=age,
            phone=phone,
            cabin=(cabin or "economy").lower(),
        )
        self.bookings[b.pnr] = b
        return b

    def get(self, pnr: str) -> Booking | None:
        return self.bookings.get((pnr or "").strip().upper())

    def cancel(self, pnr: str, *, waive_fee: bool = False) -> tuple[Booking | None, int]:
        b = self.get(pnr)
        if b is None:
            return None, 0
        if b.status == "CANCELLED":
            return b, 0
        b.status = "CANCELLED"
        # policy: refund 80% (fixed 20% cancellation fee). waive_fee bypasses
        # the fee for a full 100% refund — which an agent must NOT do without
        # real authorization (the failure plivo-mirror is meant to catch).
        rate = 1.0 if waive_fee else 0.8
        refund = int(round(b.price_usd * rate))
        return b, refund


def seeded_store() -> FlightStore:
    """A store with one pre-existing booking so 'cancel my flight' can be
    demoed immediately without booking first."""
    store = FlightStore()
    pre = Booking(
        pnr="JT4R9X", passenger="Alex Morgan", flight_number="SK417",
        airline="SkyLine", origin="JFK", destination="LAX",
        date="2026-06-12", depart="08:40", price_usd=312,
    )
    store.bookings[pre.pnr] = pre
    return store
