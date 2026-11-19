"""Deal B ledger math.

Editor accrual = price − card fee − ceil(token micros / 10000), and only when
that result is strictly positive. Connect is withheld on the payout, not in
the accrual. Platform net on a ready run is about zero when the later Connect
bill matches the withhold.
"""

from __future__ import annotations

from app.estimate import micros_to_cents


def usd(cents: int) -> str:
    """Format integer cents as a US dollar price. $15, or $13.43 when there is a remainder."""
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    if part == 0:
        return f"{sign}${whole}"
    return f"{sign}${whole}.{part:02d}"


def parse_usd(raw: str) -> int:
    """Read a dollar price into integer cents. 15 and 15.00 are 1500."""
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text or text.startswith("-") or text.count(".") > 1:
        raise ValueError("enter a price in dollars, such as 15")
    if "." in text:
        whole, frac = text.split(".", 1)
        whole = whole or "0"
        if not whole.isdigit() or not frac.isdigit() or len(frac) > 2:
            raise ValueError("enter a price in dollars, such as 15 or 15.50")
        cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    elif text.isdigit():
        cents = int(text) * 100
    else:
        raise ValueError("enter a price in dollars, such as 15")
    if cents <= 0:
        raise ValueError("enter a price in dollars, such as 15")
    return cents


def accrual_cents(price_cents: int, stripe_fee_cents: int, token_micros: int) -> int:
    net = price_cents - stripe_fee_cents - micros_to_cents(token_micros)
    return net if net > 0 else 0


def connect_withhold_cents(transfer_cents: int) -> int:
    """W = 225 + (P × 50 + 9999) // 10000.

    That is $2.00 per active account, $0.25 per payout, and 0.25% of volume,
    rounded up on the variable piece.
    """
    return 225 + (transfer_cents * 50 + 9999) // 10_000


def split_payout(balance_cents: int, minimum_cents: int = 2500) -> tuple[int, int]:
    """Largest transfer P such that P + withhold(P) does not exceed the balance."""
    if balance_cents <= minimum_cents:
        return 0, 0
    lo = 0
    hi = balance_cents
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        withheld = connect_withhold_cents(mid)
        if mid + withheld <= balance_cents:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best <= 0:
        return 0, 0
    # Raising the transfer by one cent raises the withhold by at most one cent,
    # so the unpaid remainder is zero or one cent.
    return best, connect_withhold_cents(best)
