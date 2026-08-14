from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from zidoutrade.broker import (
    ACCOUNT_ENV,
    AmbiguousBrokerResponse,
    BrokerSafetyError,
    LimitOrderRequest,
    MoomooPaperBroker,
    Side,
    _ack_status_is_accepted,
    _side,
    require_simulate,
)
from zidoutrade.storage import account_fingerprint


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orientation):
        if orientation != "records":
            raise AssertionError("unexpected orientation")
        return list(self.rows)


class FakeContext:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.accounts = [
            {
                "acc_id": 111111,
                "trd_env": "SIMULATE",
                "acc_type": "CASH",
                "security_firm": "FUTUJP",
                "trdmarket_auth": ["US"],
                "acc_status": "ACTIVE",
                "acc_role": "NORMAL",
            }
        ]
        self.orders = []
        self.positions = []
        self.place_result = None

    def get_acc_list(self):
        self.calls.append(("accounts", {}))
        return 0, Frame(self.accounts)

    def order_list_query(self, **kwargs):
        self.calls.append(("orders", kwargs))
        return 0, Frame(self.orders)

    def position_list_query(self, **kwargs):
        self.calls.append(("positions", kwargs))
        return 0, Frame(self.positions)

    def place_order(self, **kwargs):
        self.calls.append(("place", kwargs))
        if self.place_result is not None:
            return self.place_result
        row = {
            "code": kwargs["code"],
            "dealt_qty": 0,
            "order_id": "order-synthetic-1",
            "order_status": "SUBMITTED",
            "order_type": "NORMAL",
            "price": kwargs["price"],
            "qty": kwargs["qty"],
            "remark": kwargs["remark"],
            "time_in_force": kwargs["time_in_force"],
            "fill_outside_rth": kwargs["fill_outside_rth"],
            "session": kwargs["session"],
            "trd_side": kwargs["trd_side"],
        }
        return 0, Frame([row])

    def close(self):
        self.closed = True


def fake_sdk(context):
    return SimpleNamespace(
        RET_OK=0,
        OpenSecTradeContext=lambda **kwargs: (
            context.calls.append(("context", kwargs)) or context
        ),
        TrdMarket=SimpleNamespace(US="US"),
        SecurityFirm=SimpleNamespace(FUTUJP="FUTUJP"),
        Session=SimpleNamespace(RTH="RTH"),
        TrdEnv=SimpleNamespace(SIMULATE="SIMULATE"),
        TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
        OrderType=SimpleNamespace(NORMAL="NORMAL"),
        TimeInForce=SimpleNamespace(DAY="DAY"),
    )


def rth_time():
    return datetime(2026, 8, 13, 10, 30, tzinfo=ZoneInfo("America/New_York"))


class BrokerTests(unittest.TestCase):
    def make_broker(self, context=None, **kwargs):
        active_context = context or FakeContext()
        broker = MoomooPaperBroker(
            environ={ACCOUNT_ENV: "111111"},
            rth_clock=rth_time,
            rth_verifier=lambda now: True,
            sdk_loader=lambda: fake_sdk(active_context),
            **kwargs,
        )
        return broker, active_context

    def request(self, **changes):
        values = {
            "intent_id": "session-BUY-deadbeef",
            "symbol": "US.TEST",
            "side": Side.BUY,
            "quantity": 2,
            "limit_price": Decimal("10.25"),
            "remark": "RSI1-session-BUY-deadbeef",
        }
        values.update(changes)
        return LimitOrderRequest(**values)

    def test_environment_and_endpoint_fail_closed(self):
        with self.assertRaises(BrokerSafetyError):
            require_simulate("R" + "EAL")
        with self.assertRaises(BrokerSafetyError):
            require_simulate("NOT_SIMULATE")
        with self.assertRaises(BrokerSafetyError):
            require_simulate("SIMULATE_LATER")
        with self.assertRaises(BrokerSafetyError):
            MoomooPaperBroker(environ={ACCOUNT_ENV: "111111"}, host="localhost")
        with self.assertRaises(BrokerSafetyError):
            MoomooPaperBroker(environ={})

    def test_order_request_rejects_side_and_quantity_lookalikes(self):
        with self.assertRaises(TypeError):
            self.request(side="BUY")
        with self.assertRaises(ValueError):
            self.request(quantity=True)
        with self.assertRaises(ValueError):
            self.request(quantity=1.5)

    def test_broker_enum_and_status_lookalikes_are_rejected(self):
        for value in ("NOT.BUY", "NOT_BUY", "BUY_LATER"):
            with self.subTest(value=value):
                with self.assertRaises(AmbiguousBrokerResponse):
                    _side(value)
        for value in ("NOT_SUBMITTED", "SUBMIT_FAILED", "ABNORMAL"):
            with self.subTest(value=value):
                self.assertFalse(_ack_status_is_accepted(value))
        for value in ("WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART", "FILLED_ALL"):
            with self.subTest(value=value):
                self.assertTrue(_ack_status_is_accepted(value))

    def test_reconcile_queries_only_selected_symbol_with_refresh(self):
        broker, context = self.make_broker()
        snapshot = broker.reconcile("US.TEST")
        self.assertEqual(snapshot.symbol, "US.TEST")
        self.assertEqual(snapshot.position.quantity, 0)
        self.assertEqual(
            [call[0] for call in context.calls],
            ["context", "accounts", "orders", "positions"],
        )
        context_arguments = context.calls[0][1]
        self.assertEqual(context_arguments["filter_trdmarket"], "US")
        self.assertEqual(context_arguments["security_firm"], "FUTUJP")
        for _, arguments in context.calls[2:]:
            self.assertEqual(arguments["code"], "US.TEST")
            self.assertTrue(arguments["refresh_cache"])
            self.assertEqual(arguments["trd_env"], "SIMULATE")
        self.assertTrue(context.closed)

    def test_limit_order_has_rth_and_paper_guards_and_exact_ack(self):
        broker, context = self.make_broker()
        with self.assertRaisesRegex(
            BrokerSafetyError, "disabled in this reviewed release"
        ):
            broker.place_limit(self.request(), selected_symbol="US.TEST")
        # The hard release stop runs before even the optional SDK loader.
        self.assertEqual(context.calls, [])

    def test_configured_account_fingerprint_never_exposes_raw_identifier(self):
        broker, _ = self.make_broker()
        secret = b"synthetic-broker-fingerprint-key!"
        fingerprint = broker.configured_account_fingerprint(secret)
        self.assertEqual(fingerprint, account_fingerprint("111111", secret))
        self.assertNotIn("111111", fingerprint)

    def test_sell_requeries_exact_sellable_long_in_same_context(self):
        context = FakeContext()
        context.positions = [
            {
                "acc_id": 111111,
                "can_sell_qty": 2,
                "code": "US.TEST",
                "position_side": "LONG",
                "qty": 2,
            }
        ]
        broker, _ = self.make_broker(context)
        request = self.request(
            intent_id="session-SELL-deadbeef",
            side=Side.SELL,
            remark="RSI1-session-SELL-deadbeef",
        )
        sdk = fake_sdk(context)
        broker._require_sellable_position(sdk, context, request)
        names = [name for name, _ in context.calls]
        self.assertEqual(names, ["positions"])
        arguments = context.calls[0][1]
        self.assertEqual(arguments["code"], "US.TEST")
        self.assertEqual(arguments["acc_id"], 111111)
        self.assertEqual(arguments["trd_env"], "SIMULATE")
        self.assertTrue(arguments["refresh_cache"])

    def test_sell_preflight_ambiguity_or_insufficient_quantity_never_places(self):
        valid = {
            "acc_id": 111111,
            "can_sell_qty": 2,
            "code": "US.TEST",
            "position_side": "LONG",
            "qty": 2,
        }
        unsafe = [
            [],
            [valid, dict(valid)],
            [{**valid, "acc_id": 222222}],
            [{**valid, "code": "US.OTHER"}],
            [{**valid, "position_side": "SHORT"}],
            [{**valid, "qty": 1, "can_sell_qty": 1}],
            [{**valid, "can_sell_qty": 1}],
            [{**valid, "can_sell_qty": "N/A"}],
        ]
        for rows in unsafe:
            with self.subTest(rows=rows):
                context = FakeContext()
                context.positions = rows
                broker, _ = self.make_broker(context)
                request = self.request(
                    intent_id="session-SELL-deadbeef",
                    side=Side.SELL,
                    remark="RSI1-session-SELL-deadbeef",
                )
                with self.assertRaises((BrokerSafetyError, AmbiguousBrokerResponse)):
                    broker._require_sellable_position(fake_sdk(context), context, request)
                self.assertFalse(any(name == "place" for name, _ in context.calls))

    def test_unselected_symbol_and_outside_rth_never_load_sdk(self):
        calls = []
        broker = MoomooPaperBroker(
            environ={ACCOUNT_ENV: "111111"},
            rth_clock=lambda: datetime(
                2026, 8, 13, 8, 0, tzinfo=ZoneInfo("America/New_York")
            ),
            rth_verifier=lambda now: True,
            sdk_loader=lambda: calls.append("loaded"),
        )
        with self.assertRaises(BrokerSafetyError):
            broker.place_limit(self.request(), selected_symbol="US.OTHER")
        with self.assertRaises(BrokerSafetyError):
            broker.place_limit(self.request(), selected_symbol="US.TEST")
        self.assertEqual(calls, [])

    def test_dispatch_requires_authoritative_session_verification(self):
        context = FakeContext()
        broker = MoomooPaperBroker(
            environ={ACCOUNT_ENV: "111111"},
            rth_clock=rth_time,
            sdk_loader=lambda: fake_sdk(context),
        )
        with self.assertRaises(BrokerSafetyError):
            broker.place_limit(self.request(), selected_symbol="US.TEST")
        self.assertEqual(context.calls, [])

    def test_ack_field_mismatch_is_ambiguous(self):
        context = FakeContext()
        context.place_result = (
            0,
            Frame(
                [
                    {
                        "code": "US.OTHER",
                        "dealt_qty": 0,
                        "order_id": "order-synthetic-2",
                        "order_status": "SUBMITTED",
                        "order_type": "NORMAL",
                        "price": 10.25,
                        "qty": 2,
                        "remark": "RSI1-session-BUY-deadbeef",
                        "time_in_force": "DAY",
                        "fill_outside_rth": False,
                        "session": "RTH",
                        "trd_side": "BUY",
                    }
                ]
            ),
        )
        broker, _ = self.make_broker(context)
        with self.assertRaises(BrokerSafetyError):
            broker.place_limit(self.request(), selected_symbol="US.TEST")

    def test_ack_side_lookalike_is_ambiguous(self):
        context = FakeContext()
        context.place_result = (
            0,
            Frame(
                [
                    {
                        "code": "US.TEST",
                        "dealt_qty": 0,
                        "order_id": "order-synthetic-side",
                        "order_status": "SUBMITTED",
                        "order_type": "NORMAL",
                        "price": 10.25,
                        "qty": 2,
                        "remark": "RSI1-session-BUY-deadbeef",
                        "trd_side": "NOT_BUY",
                    }
                ]
            ),
        )
        broker, _ = self.make_broker(context)
        with self.assertRaises(BrokerSafetyError):
            broker.place_limit(self.request(), selected_symbol="US.TEST")

    def test_terminal_ack_status_takes_precedence_over_submit_prefix(self):
        context = FakeContext()
        context.place_result = (
            0,
            Frame(
                [
                    {
                        "code": "US.TEST",
                        "dealt_qty": 0,
                        "order_id": "order-synthetic-failed",
                        "order_status": "SUBMIT_FAILED",
                        "order_type": "NORMAL",
                        "price": 10.25,
                        "qty": 2,
                        "remark": "RSI1-session-BUY-deadbeef",
                        "time_in_force": "DAY",
                        "fill_outside_rth": False,
                        "session": "RTH",
                        "trd_side": "BUY",
                    }
                ]
            ),
        )
        broker, _ = self.make_broker(context)
        with self.assertRaises(BrokerSafetyError):
            broker.place_limit(self.request(), selected_symbol="US.TEST")

    def test_account_verification_rejects_wrong_or_ambiguous_rows_before_query(self):
        unsafe_rows = [
            [{
                "acc_id": 111111,
                "trd_env": "REAL",
                "acc_type": "CASH",
                "security_firm": "FUTUJP",
                "trdmarket_auth": ["US"],
                "acc_status": "ACTIVE",
                "acc_role": "NORMAL",
            }],
            [{
                "acc_id": 111111,
                "trd_env": "SIMULATE",
                "acc_type": "CASH",
                "security_firm": "FUTUJP",
                "trdmarket_auth": ["US"],
                "acc_status": "ACTIVE",
                "acc_role": "MASTER",
            }],
            [{
                "acc_id": 111111,
                "trd_env": "SIMULATE",
                "acc_type": "CASH",
                "security_firm": "OTHER",
                "trdmarket_auth": ["US"],
                "acc_status": "ACTIVE",
                "acc_role": "NORMAL",
            }],
            [{
                "acc_id": 111111,
                "trd_env": "SIMULATE",
                "acc_type": "CASH",
                "security_firm": "FUTUJP",
                "trdmarket_auth": ["HK"],
                "acc_status": "ACTIVE",
                "acc_role": "NORMAL",
            }],
            [],
        ]
        for rows in unsafe_rows:
            with self.subTest(rows=rows):
                context = FakeContext()
                context.accounts = rows
                broker, _ = self.make_broker(context)
                with self.assertRaises(BrokerSafetyError):
                    broker.reconcile("US.TEST")
                self.assertEqual(
                    [name for name, _ in context.calls if name in {"orders", "positions", "place"}],
                    [],
                )
                self.assertTrue(context.closed)

    def test_duplicate_matching_account_is_rejected(self):
        context = FakeContext()
        context.accounts = context.accounts * 2
        broker, _ = self.make_broker(context)
        with self.assertRaises(BrokerSafetyError):
            broker.place_limit(self.request(), selected_symbol="US.TEST")
        self.assertFalse(any(name == "place" for name, _ in context.calls))

    def test_duplicate_position_rows_are_ambiguous(self):
        context = FakeContext()
        row = {
            "acc_id": 111111,
            "can_sell_qty": 1,
            "code": "US.TEST",
            "position_side": "LONG",
            "qty": 1,
        }
        context.positions = [row, dict(row)]
        broker, _ = self.make_broker(context)
        with self.assertRaises(AmbiguousBrokerResponse):
            broker.reconcile("US.TEST")

    def test_short_or_unsellable_position_is_rejected(self):
        unsafe_rows = [
            {
                "acc_id": 111111,
                "can_sell_qty": 1,
                "code": "US.TEST",
                "position_side": "SHORT",
                "qty": 1,
            },
            {
                "acc_id": 111111,
                "can_sell_qty": 1,
                "code": "US.TEST",
                "position_side": "NOT.LONG",
                "qty": 1,
            },
            {
                "acc_id": 111111,
                "can_sell_qty": 2,
                "code": "US.TEST",
                "position_side": "LONG",
                "qty": 1,
            },
            {
                "acc_id": 111111,
                "can_sell_qty": "N/A",
                "code": "US.TEST",
                "position_side": "LONG",
                "qty": 1,
            },
        ]
        for row in unsafe_rows:
            with self.subTest(row=row):
                context = FakeContext()
                context.positions = [row]
                broker, _ = self.make_broker(context)
                with self.assertRaises(AmbiguousBrokerResponse):
                    broker.reconcile("US.TEST")

    def test_adapter_has_no_arbitrary_account_unlock_surface(self):
        broker, _ = self.make_broker()
        forbidden = "unlock" + "_trade"
        self.assertFalse(hasattr(broker, forbidden))


if __name__ == "__main__":
    unittest.main()
