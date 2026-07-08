from decimal import Decimal

from app.engine.commission_ledger import OrderEvent, allocate_lifecycle_commission


def ev(intent_id, kind, qty, commission="0", pnl="0"):
    return OrderEvent(intent_id=intent_id, kind=kind, qty=Decimal(qty),
                       commission=Decimal(commission), realized_pnl=Decimal(pnl))


def test_single_entry_then_close():
    events = [
        ev(1, "entry", "0.02", commission="0.01"),
        ev(2, "close", "0.02", commission="0.02", pnl="5.0"),
    ]
    result = allocate_lifecycle_commission(events)
    assert 1 not in result  # pure entry, never closes -> no attribution row
    assert result[2].attributed_commission == Decimal("0.03")  # 0.01 (ancestor entry) + 0.02 (own exit)
    assert result[2].realized_pnl == Decimal("5.0")
    assert result[2].has_close is True


def test_chain_of_adds_then_partial_reduce_then_final_close():
    # A: add 0.02 (comm 0.01) -> B: add 0.02 (comm 0.01) -> C: partial reduce 0.01
    # (comm 0.005) -> D: final close remaining 0.03 (comm 0.015). No double-
    # counting of A/B's entry commission across C and D.
    events = [
        ev("A", "entry", "0.02", commission="0.01"),
        ev("B", "entry", "0.02", commission="0.01"),
        ev("C", "close", "0.01", commission="0.005", pnl="1.0"),
        ev("D", "close", "0.03", commission="0.015", pnl="3.0"),
    ]
    result = allocate_lifecycle_commission(events)
    assert result["C"].attributed_commission == Decimal("0.01")   # 0.005 ancestor-share + 0.005 own
    assert result["D"].attributed_commission == Decimal("0.03")   # 0.015 ancestor-share + 0.015 own
    # invariant: total attributed across all closes == total commission paid
    # in the whole lifecycle (entries + closes), no gap, no double-count.
    total_paid = Decimal("0.01") + Decimal("0.01") + Decimal("0.005") + Decimal("0.015")
    total_attributed = result["C"].attributed_commission + result["D"].attributed_commission
    assert total_attributed == total_paid


def test_flip_does_not_leak_commission_between_lifecycles():
    # A: add 0.02 LONG (comm 0.01) -> B (flip): closes the 0.02 LONG (comm 0.01,
    # pnl 2.0) THEN opens 0.03 SHORT (comm 0.015) in the same intent -> C:
    # closes the 0.03 SHORT (comm 0.015, pnl -0.5).
    events = [
        ev("A", "entry", "0.02", commission="0.01"),
        ev("B", "close", "0.02", commission="0.01", pnl="2.0"),
        ev("B", "entry", "0.03", commission="0.015"),
        ev("C", "close", "0.03", commission="0.015", pnl="-0.5"),
    ]
    result = allocate_lifecycle_commission(events)
    # B's attribution reflects ONLY the old (LONG) lifecycle it closed:
    # A's entry (0.01) + B's own close commission (0.01) = 0.02 -- not
    # inflated by B's own new SHORT entry commission (0.015).
    assert result["B"].attributed_commission == Decimal("0.02")
    assert result["B"].realized_pnl == Decimal("2.0")
    # C's attribution reflects ONLY the new (SHORT) lifecycle B opened:
    # B's entry (0.015) + C's own close commission (0.015) = 0.03 -- no
    # leakage from the old LONG lifecycle already settled via B.
    assert result["C"].attributed_commission == Decimal("0.03")
    assert result["C"].realized_pnl == Decimal("-0.5")


def test_failed_intent_partial_fill_commission_not_dropped():
    # A, B add up a position (comm 0.01 each). C is an intent whose OWN
    # state ended up FAILED (e.g. exhausted reduce retries) but DID manage to
    # partially close 0.01 for real (a real FILLED order) before giving up.
    # This function only looks at filled orders, never at intent.state, so
    # C's real commission must still be consumed correctly. D later closes
    # the rest.
    events = [
        ev("A", "entry", "0.02", commission="0.01"),
        ev("B", "entry", "0.02", commission="0.01"),
        ev("C", "close", "0.01", commission="0.005", pnl="0.2"),  # C's intent.state == FAILED, irrelevant here
        ev("D", "close", "0.03", commission="0.015", pnl="1.0"),
    ]
    result = allocate_lifecycle_commission(events)
    total_entry = Decimal("0.01") + Decimal("0.01")
    ancestor_share_c = result["C"].attributed_commission - Decimal("0.005")  # strip C's own commission
    ancestor_share_d = result["D"].attributed_commission - Decimal("0.015")
    assert ancestor_share_c + ancestor_share_d == total_entry  # nothing dropped


def test_multi_fill_close_within_one_intent_sums_not_overwrites():
    # A single intent's reduce-retry loop can produce TWO separate FILLED
    # close-role orders. realized_pnl must sum across both, not just keep
    # the last (the pre-existing get_closing_fill bug this replaces).
    events = [
        ev("A", "entry", "0.02", commission="0.01"),
        ev("B", "close", "0.01", commission="0.005", pnl="0.3"),
        ev("B", "close", "0.01", commission="0.005", pnl="0.4"),
    ]
    result = allocate_lifecycle_commission(events)
    assert result["B"].realized_pnl == Decimal("0.3") + Decimal("0.4")
    assert result["B"].attributed_commission == Decimal("0.01") + Decimal("0.01")  # 0.005+0.005 ancestor + 0.005+0.005 own
    assert result["B"].has_close is True


def test_close_larger_than_basis_does_not_go_negative():
    # Defensive: a close qty slightly exceeding the tracked basis (shouldn't
    # happen with real exchange data, but the function must not corrupt state
    # for whatever comes after).
    events = [
        ev("A", "entry", "0.02", commission="0.01"),
        ev("B", "close", "0.025", commission="0.0125", pnl="1.0"),  # overshoots basis_qty
        ev("C", "entry", "0.02", commission="0.01"),
        ev("D", "close", "0.02", commission="0.01", pnl="0.5"),
    ]
    result = allocate_lifecycle_commission(events)
    assert result["B"].attributed_commission == Decimal("0.01") + Decimal("0.0125")  # all remaining basis + own
    # C/D form a fresh, uncorrupted lifecycle after B clamps the basis to 0
    assert result["D"].attributed_commission == Decimal("0.01") + Decimal("0.01")
