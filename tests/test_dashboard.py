from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import socket
import threading
from types import SimpleNamespace
import unittest

from zidoutrade import dashboard
from zidoutrade.dashboard import DashboardApplication, DashboardServer
from zidoutrade.risk_settings import (
    RiskSettingsConflictError,
    default_public_risk_settings,
)
from zidoutrade.selection import PresentedCandidate, SelectionWorkflow


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.state = {
            "overview": {"target_session": "2026-08-14"},
            "candidates": [
                {
                    "symbol": "US.AAPL",
                    "priority": 1,
                    "eligible": True,
                    "status": "PASS",
                    "reason_codes": [],
                },
                {
                    "symbol": "US.BAD",
                    "priority": 2,
                    "eligible": False,
                    "status": "FAIL",
                    "reason_codes": ["TRADING_HALTED"],
                },
            ],
            "selection": None,
            "decision": {"action": "WAIT", "reason": "NO_SIGNAL"},
            "risk": {"planned_risk": "0.25%"},
            "journal": [],
            "system": {"mode": "SIMULATE_ONLY"},
        }

        def callback(symbol, target_session):
            cls.calls.append((symbol, target_session))
            return {"saved": True, "selected_symbol": symbol, "target_session": target_session}

        cls.token = "c" * 48
        cls.application = DashboardApplication(
            lambda: cls.state,
            callback,
            validation_callback=lambda expected: {
                "saved": True,
                "selected_symbol": "US.AAPL",
                "target_session": "2026-08-14",
                "state": "VALIDATED",
                "record_sha256": "b" * 64,
            },
            arming_callback=lambda expected, confirmation: {
                "saved": True,
                "selected_symbol": "US.AAPL",
                "target_session": "2026-08-14",
                "state": "ARMED_NEXT_SESSION",
                "record_sha256": "a" * 64,
            },
            csrf_token=cls.token,
        )
        cls.host = "127.0.0.1"
        cls.port = 8765
        cls.handler_class = dashboard._handler_class(cls.application)

    def setUp(self):
        self.calls.clear()

    def request(self, method, path, body=None, headers=None):
        raw_body = b"" if body is None else body.encode("utf-8") if isinstance(body, str) else body
        sent_headers = dict(headers or {})
        sent_headers.setdefault("Host", self.authority)
        if raw_body and "Content-Length" not in sent_headers:
            sent_headers["Content-Length"] = str(len(raw_body))
        request_head = "%s %s HTTP/1.0\r\n%s\r\n\r\n" % (
            method,
            path,
            "\r\n".join("%s: %s" % item for item in sent_headers.items()),
        )
        server_socket, client_socket = socket.socketpair()
        errors = []

        def serve_one():
            try:
                fake_server = SimpleNamespace(server_name="localhost", server_port=self.port)
                self.handler_class(server_socket, ("127.0.0.1", 12345), fake_server)
            except BaseException as exc:  # propagate handler failures to the test thread
                errors.append(exc)
            finally:
                try:
                    server_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                server_socket.close()

        try:
            client_socket.sendall(request_head.encode("ascii") + raw_body)
            client_socket.shutdown(socket.SHUT_WR)
            thread = threading.Thread(target=serve_one, daemon=True)
            thread.start()
            chunks = []
            while True:
                chunk = client_socket.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)
            thread.join(timeout=2)
            if thread.is_alive():
                self.fail("in-memory HTTP handler did not terminate")
            if errors:
                raise errors[0]
        finally:
            server_socket.close()
            client_socket.close()
        head, payload = response.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        response_headers = {}
        for line in lines[1:]:
            key, value = line.split(":", 1)
            response_headers[key] = value.strip()
        return status, response_headers, payload

    @property
    def authority(self):
        return "%s:%d" % (self.host, self.port)

    def valid_headers(self):
        return {
            "Host": self.authority,
            "Origin": "http://" + self.authority,
            "X-CSRF-Token": self.token,
            "Content-Type": "application/json",
        }

    def post_selection(self, value, headers=None):
        body = json.dumps(value, separators=(",", ":"))
        return self.request("POST", "/api/selection", body=body, headers=headers or self.valid_headers())

    def request_on(self, application, method, path, body=None, headers=None):
        original = self.handler_class
        self.handler_class = dashboard._handler_class(application)
        try:
            return self.request(method, path, body=body, headers=headers)
        finally:
            self.handler_class = original

    @staticmethod
    def risk_payload(**changes):
        value = {
            "confirmation": "SAVE_NEXT_SESSION_RISK",
            "daily_loss_limit_basis_points": 75,
            "expected_sha256": None,
            "maximum_investment_cents": 50_000,
            "planned_risk_basis_points": 25,
            "risk_policy_version": "RSI_RISK_POLICY_V2",
            "target_session": "2026-08-14",
            "weekly_loss_limit_basis_points": 200,
        }
        value.update(changes)
        return value

    def test_static_ui_has_sidebar_sections_and_explains_rsi_rules(self):
        status, headers, body = self.request("GET", "/", headers={"Host": self.authority})
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        for section in ("overview", "candidates", "decision", "risk", "journal", "system"):
            self.assertIn('id="%s"' % section, html)
        self.assertIn("Wilder RSI(14)", html)
        self.assertIn("RSI ≤ 30", html)
        self.assertIn("1.5 ATR", html)
        self.assertIn("SHADOW / 注文機能は無効", html)
        self.assertIn("SIMULATE注文も常に拒否", html)
        for wording in (
            "出来高 1.5倍以上",
            "直前13本の中央値",
            "スプレッド 0.10%以下",
            "追いかけ幅 0.50%以下",
            "現在の保守的な設定",
            "変更できる絶対上限",
            "最大 1%",
            "最大 2%",
            "最大 5%",
            "損失額を保証する上限ではありません",
            "買い代金 + 買い手数料",
            "保存しても注文機能は有効になりません",
            "Q013研究候補：ATR比0.50%以下",
            "ATR ÷ 終値",
            "本番未採用",
            "本番未採用・注文は0件です",
            "記録機能を未接続",
        ):
            self.assertIn(wording, html)
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_beginner_ui_leads_with_next_action_and_plain_japanese(self):
        status, _, body = self.request("GET", "/", headers={"Host": self.authority})
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        for wording in (
            "まず、ここから",
            "候補を見る",
            "この画面は練習用です。お金は動きません",
            "分析する銘柄を1つ選ぶ",
            "分析対象に確定",
            "システムの判断を見る",
            "損失を抑えるルール",
            "判断の記録を振り返る",
            "RSI・ATRなどの用語をやさしく確認",
        ):
            self.assertIn(wording, html)
        self.assertNotIn("ORDER DISABLED", html)
        self.assertNotIn(">DRAFT<", html)
        self.assertNotIn(">SELECTION ARMED<", html)

        status, _, script = self.request("GET", "/app.js", headers={"Host": self.authority})
        self.assertEqual(status, 200)
        javascript = script.decode("utf-8")
        self.assertIn('WAIT: "待機中"', javascript)
        self.assertIn('ENTER: "買い条件が成立"', javascript)
        self.assertIn('EXIT: "売り条件が成立"', javascript)
        self.assertIn('node.setAttribute("aria-current", "page")', javascript)
        self.assertNotIn(".innerHTML", javascript)
        self.assertIn('fetch("/api/risk-settings"', javascript)
        self.assertIn('confirmation: "SAVE_NEXT_SESSION_RISK"', javascript)
        for code in (
            "VOLUME_HISTORY_INSUFFICIENT",
            "VOLUME_DATA_INVALID",
            "VOLUME_CONFIRMATION_MISSING",
            "PRICE_REFERENCE_INVALID",
            "PRICE_CONFIRMATION_MISSING",
            "BREAKOUT_TOO_EXTENDED",
            "SPREAD_TOO_WIDE",
        ):
            self.assertIn(code, javascript)

    def test_risk_settings_state_is_read_only_without_mutation_callback(self):
        status, _, body = self.request("GET", "/api/state", headers={"Host": self.authority})
        self.assertEqual(status, 200, body)
        settings = json.loads(body)["risk_settings"]
        self.assertFalse(settings["editable"])
        self.assertTrue(settings["entry_blocked"])
        self.assertIsNone(settings["maximum_investment_cents"])
        self.assertEqual(settings["planned_risk_basis_points"], 25)
        self.assertEqual(settings["daily_loss_limit_basis_points"], 75)
        self.assertEqual(settings["weekly_loss_limit_basis_points"], 200)

        request_body = json.dumps(self.risk_payload(), separators=(",", ":"))
        status, _, response = self.request(
            "POST",
            "/api/risk-settings",
            body=request_body,
            headers=self.valid_headers(),
        )
        self.assertEqual(status, 503, response)
        self.assertEqual(json.loads(response)["error"], "RISK_SETTINGS_READ_ONLY")

    def test_strict_risk_settings_post_passes_typed_update_to_narrow_callback(self):
        calls = []

        def callback(update):
            calls.append(update)
            return {
                "revision": 1,
                "saved": True,
                "target_session": update.target_session,
            }

        app = DashboardApplication(
            lambda: self.state,
            risk_settings_provider=lambda: default_public_risk_settings(editable=True),
            risk_settings_callback=callback,
            csrf_token=self.token,
        )
        raw = json.dumps(self.risk_payload(), separators=(",", ":"))
        status, _, body = self.request_on(
            app,
            "POST",
            "/api/risk-settings",
            body=raw,
            headers=self.valid_headers(),
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(calls), 1)
        update = calls[0]
        self.assertEqual(update.policy.maximum_investment_cents, 50_000)
        self.assertEqual(update.policy.planned_risk_basis_points, 25)
        self.assertEqual(
            json.loads(body),
            {
                "message": "Risk settings revision saved for a future session.",
                "revision": 1,
                "saved": True,
                "target_session": "2026-08-14",
            },
        )

    def test_risk_settings_reject_malformed_types_ranges_order_and_stale_revision(self):
        calls = []

        def callback(update):
            calls.append(update)
            if update.expected_sha256 == "f" * 64:
                raise RiskSettingsConflictError("synthetic stale revision")
            return {"revision": 2, "saved": True, "target_session": update.target_session}

        app = DashboardApplication(
            lambda: self.state,
            risk_settings_provider=lambda: default_public_risk_settings(editable=True),
            risk_settings_callback=callback,
            csrf_token=self.token,
        )
        attacks = (
            {**self.risk_payload(), "admin": True},
            self.risk_payload(planned_risk_basis_points=True),
            self.risk_payload(daily_loss_limit_basis_points=201),
            self.risk_payload(weekly_loss_limit_basis_points=501),
            self.risk_payload(
                planned_risk_basis_points=80,
                daily_loss_limit_basis_points=75,
            ),
            self.risk_payload(maximum_investment_cents=0),
            self.risk_payload(risk_policy_version="V3"),
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                raw = json.dumps(attack, separators=(",", ":"))
                status, _, body = self.request_on(
                    app,
                    "POST",
                    "/api/risk-settings",
                    body=raw,
                    headers=self.valid_headers(),
                )
                self.assertEqual(status, 422, body)
        self.assertEqual(calls, [])

        stale = json.dumps(
            self.risk_payload(expected_sha256="f" * 64), separators=(",", ":")
        )
        status, _, body = self.request_on(
            app,
            "POST",
            "/api/risk-settings",
            body=stale,
            headers=self.valid_headers(),
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["error"], "RISK_SETTINGS_CONFLICT")

    def test_risk_settings_security_checks_run_before_callback(self):
        calls = []
        app = DashboardApplication(
            lambda: self.state,
            risk_settings_provider=lambda: default_public_risk_settings(editable=True),
            risk_settings_callback=lambda update: calls.append(update),
            csrf_token=self.token,
        )
        raw = json.dumps(self.risk_payload(), separators=(",", ":"))
        bad_headers = []
        malicious_host = self.valid_headers()
        malicious_host["Host"] = "attacker.example"
        bad_headers.append(malicious_host)
        malicious_origin = self.valid_headers()
        malicious_origin["Origin"] = "http://attacker.example"
        bad_headers.append(malicious_origin)
        bad_csrf = self.valid_headers()
        bad_csrf["X-CSRF-Token"] = "wrong"
        bad_headers.append(bad_csrf)
        for headers in bad_headers:
            status, _, _ = self.request_on(
                app,
                "POST",
                "/api/risk-settings",
                body=raw,
                headers=headers,
            )
            self.assertEqual(status, 403)
        self.assertEqual(calls, [])

    def test_provider_cannot_spoof_editability_and_large_investment_is_redacted_safely(self):
        settings = default_public_risk_settings(editable=True)
        settings.update(
            {
                "entry_blocked": False,
                "maximum_investment_cents": 1_000_000,
                "revision": 1,
                "saved": True,
                "sha256": "e" * 64,
                "target_session": "2026-08-14",
            }
        )
        app = DashboardApplication(
            lambda: self.state,
            risk_settings_provider=lambda: settings,
            csrf_token=self.token,
        )
        public = app.state()["risk_settings"]
        self.assertFalse(public["editable"])
        self.assertEqual(public["maximum_investment_cents"], 1_000_000)

        too_large = dict(settings)
        too_large["maximum_investment_cents"] = 9_007_199_254_740_992
        with self.assertRaises(ValueError):
            DashboardApplication(
                lambda: self.state,
                risk_settings_provider=lambda: too_large,
                csrf_token=self.token,
            ).state()

    def test_state_and_csrf_are_readable_only_through_loopback_host(self):
        status, _, body = self.request("GET", "/api/state", headers={"Host": self.authority})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["system"]["mode"], "SIMULATE_ONLY")

        status, _, body = self.request("GET", "/api/csrf", headers={"Host": self.authority})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf_token"], self.token)

        status, _, body = self.request("GET", "/api/state", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "LOOPBACK_HOST_REQUIRED")

    def test_valid_selection_is_normalized_and_no_trade_is_supported(self):
        status, _, body = self.post_selection(
            {"selected_symbol": "aapl", "target_session": "2026-08-14"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.calls, [("US.AAPL", "2026-08-14")])

        status, _, body = self.post_selection(
            {"selected_symbol": None, "target_session": "2026-08-14"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.calls[-1], (None, "2026-08-14"))

    def test_validate_and_arm_are_separate_csrf_protected_transitions(self):
        headers = self.valid_headers()
        validate_body = json.dumps({"expected_sha256": "d" * 64})
        status, _, body = self.request(
            "POST", "/api/selection/validate", body=validate_body, headers=headers
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["state"], "VALIDATED")

        arm_body = json.dumps(
            {"expected_sha256": "v" * 64, "confirmation": "ARM_NEXT_SESSION"}
        )
        status, _, body = self.request(
            "POST", "/api/selection/arm", body=arm_body, headers=headers
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["state"], "ARMED_NEXT_SESSION")

        unsafe = self.valid_headers()
        unsafe["X-CSRF-Token"] = "wrong"
        status, _, _ = self.request(
            "POST", "/api/selection/arm", body=arm_body, headers=unsafe
        )
        self.assertEqual(status, 403)

    def test_malicious_host_origin_and_csrf_are_rejected_before_callback(self):
        body = '{"selected_symbol":"US.AAPL","target_session":"2026-08-14"}'
        headers = self.valid_headers()
        headers["Host"] = "evil.example"
        status, _, response = self.request("POST", "/api/selection", body=body, headers=headers)
        self.assertEqual(status, 403, response)

        headers = self.valid_headers()
        headers["Origin"] = "http://evil.example"
        status, _, response = self.request("POST", "/api/selection", body=body, headers=headers)
        self.assertEqual(status, 403, response)

        headers = self.valid_headers()
        headers["X-CSRF-Token"] = "wrong-token"
        status, _, response = self.request("POST", "/api/selection", body=body, headers=headers)
        self.assertEqual(status, 403, response)
        self.assertEqual(self.calls, [])

    def test_exact_json_schema_content_type_and_candidate_eligibility_are_enforced(self):
        status, _, _ = self.post_selection(
            {"selected_symbol": "US.AAPL", "target_session": "2026-08-14", "admin": True}
        )
        self.assertEqual(status, 400)

        headers = self.valid_headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        status, _, _ = self.post_selection(
            {"selected_symbol": "US.AAPL", "target_session": "2026-08-14"}, headers=headers
        )
        self.assertEqual(status, 415)

        status, _, body = self.post_selection(
            {"selected_symbol": "US.BAD", "target_session": "2026-08-14"}
        )
        self.assertEqual(status, 422, body)
        self.assertEqual(json.loads(body)["error"], "SELECTION_NOT_ELIGIBLE")
        self.assertEqual(self.calls, [])

    def test_duplicate_json_keys_and_noncanonical_date_are_rejected(self):
        body = (
            '{"selected_symbol":"US.AAPL","selected_symbol":null,'
            '"target_session":"2026-08-14"}'
        )
        status, _, _ = self.request("POST", "/api/selection", body=body, headers=self.valid_headers())
        self.assertEqual(status, 400)
        status, _, _ = self.post_selection(
            {"selected_symbol": "US.AAPL", "target_session": "2026-8-14"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])

    def test_no_account_or_order_api_is_exposed_and_nonloopback_bind_is_refused(self):
        for path in ("/api/orders", "/api/account", "/api/positions", "/api/activate"):
            status, _, _ = self.request("GET", path, headers={"Host": self.authority})
            self.assertEqual(status, 404)
        with self.assertRaisesRegex(ValueError, "DASHBOARD_MUST_BIND_TO_LOOPBACK"):
            DashboardServer(self.application, host="0.0.0.0", port=0)

    def test_state_provider_cannot_publish_sensitive_fields(self):
        base = dict(self.state)
        for forbidden in (
            {"system": {"account_id": "synthetic"}},
            {"system": {"broker_account_id": "synthetic"}},
            {"system": {"api_token": "synthetic"}},
            {"system": {"password_hint": "synthetic"}},
            {"decision": {"order_id": "synthetic"}},
            {"risk": {"daily_pnl": 1}},
            {"risk": {"daily_pnl_usd": 1}},
            {"overview": {"last_price": 10}},
            {"overview": {"entry_price_usd": 10}},
        ):
            value = dict(base)
            value.update(forbidden)
            application = DashboardApplication(
                lambda current=value: current,
                csrf_token="d" * 48,
            )
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValueError):
                    application.state()

    def test_sensitive_identifiers_are_rejected_when_hidden_in_public_values(self):
        attacks = (
            {"overview": {"message": "runtime reference 111111"}},
            {"decision": {"reason": "account-id: synthetic-value"}},
            {"decision": {"reason": "orderId=paper-order"}},
            {"risk": {"reason": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"}},
            {"system": {"message": "fingerprint " + "a" * 64}},
            {"system": {"message": "execution_hash=opaque-value"}},
            {"system": {"message": "remark=broker-correlation"}},
            {
                "journal": [
                    {"event": "SAFETY", "detail": "active_intent_id: intent-7"}
                ]
            },
            {
                "journal": [
                    {
                        "event": "SAFETY",
                        "note": "correlation 00000000-0000-4000-8000-000000000000",
                    }
                ]
            },
            {"overview": {"count": 111111}},
        )
        for attack in attacks:
            value = copy.deepcopy(self.state)
            value.update(attack)
            application = DashboardApplication(
                lambda current=value: current,
                csrf_token="d" * 48,
            )
            with self.subTest(attack=attack):
                with self.assertRaises(ValueError):
                    application.state()

    def test_private_journal_free_text_is_never_a_public_field(self):
        private_entry = {
            "decision": "WAIT",
            "kind": "MANUAL_NOTE",
            "mood": "CALM",
            "note": "paper-order-abc",
            "reason_codes": ["NO_SIGNAL"],
            "schema_version": "RSI_STOCK_JOURNAL_V1",
            "session_date": "2026-08-14",
            "symbol": "US.AAPL",
            "tags": ["REVIEW"],
        }
        direct = copy.deepcopy(self.state)
        direct["journal"] = [private_entry]
        with self.assertRaises(ValueError):
            DashboardApplication(lambda: direct, csrf_token="d" * 48).state()

        snapshot = dashboard.DashboardSnapshot(
            candidates=dashboard.CandidateBatch(evaluations=()),
            journal=(private_entry,),
        ).to_dict()
        self.assertNotIn("note", snapshot["journal"][0])
        DashboardApplication(lambda: snapshot, csrf_token="d" * 48).state()

    def test_public_state_rejects_untyped_or_oversized_unstructured_values(self):
        attacks = (
            {"overview": "not-an-object"},
            {"journal": ["private raw event"]},
            {"system": {"message": b"not-json"}},
            {"risk": {"ratio": float("nan")}},
            {"decision": {"reason": "x" * 2049}},
        )
        for attack in attacks:
            value = copy.deepcopy(self.state)
            value.update(attack)
            application = DashboardApplication(
                lambda current=value: current,
                csrf_token="d" * 48,
            )
            with self.subTest(attack=repr(attack)):
                with self.assertRaises(ValueError):
                    application.state()

    def test_only_valid_selection_chain_hash_fields_may_contain_sha256(self):
        draft = SelectionWorkflow.new_draft(
            target_session="2026-08-14",
            presented_candidates=(PresentedCandidate("US.AAPL", 1, True),),
            selected_symbol="US.AAPL",
            now=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
        validated = SelectionWorkflow.validate(
            draft,
            now=datetime(2026, 8, 13, 1, 1, tzinfo=timezone.utc),
        )
        value = copy.deepcopy(self.state)
        value["selection"] = validated.envelope()
        application = DashboardApplication(lambda: value, csrf_token="d" * 48)
        self.assertEqual(application.state()["selection"]["sha256"], validated.sha256)

        value["selection"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            application.state()

    def test_callback_message_cannot_exfiltrate_an_order_identifier(self):
        for unsafe_message in ("order_id=777777777", "paper-order-abc"):
            application = DashboardApplication(
                lambda: self.state,
                selection_callback=lambda symbol, target, message=unsafe_message: {
                    "saved": True,
                    "selected_symbol": symbol,
                    "target_session": target,
                    "message": message,
                },
                csrf_token=self.token,
            )
            previous_handler = self.handler_class
            self.handler_class = dashboard._handler_class(application)
            try:
                status, _, body = self.post_selection(
                    {"selected_symbol": "US.AAPL", "target_session": "2026-08-14"}
                )
            finally:
                self.handler_class = previous_handler
            with self.subTest(message=unsafe_message):
                self.assertEqual(status, 200, body)
                self.assertEqual(
                    json.loads(body),
                    {
                        "message": "Selection revision saved.",
                        "saved": True,
                        "selected_symbol": "US.AAPL",
                        "target_session": "2026-08-14",
                    },
                )

    def test_callback_record_hash_is_never_published(self):
        application = DashboardApplication(
            lambda: self.state,
            selection_callback=lambda symbol, target: {
                "saved": True,
                "selected_symbol": symbol,
                "target_session": target,
                "record_sha256": "a" * 64,
            },
            csrf_token=self.token,
        )
        previous_handler = self.handler_class
        self.handler_class = dashboard._handler_class(application)
        try:
            status, _, body = self.post_selection(
                {"selected_symbol": "US.AAPL", "target_session": "2026-08-14"}
            )
        finally:
            self.handler_class = previous_handler
        self.assertEqual(status, 200, body)
        self.assertNotIn("record_sha256", json.loads(body))

    def test_public_sections_reject_unapproved_identifier_fields(self):
        for attack in (
            {"decision": {"id": "paper-order-abc"}},
            {"system": {"reference": "opaque123"}},
            {"overview": {"identifier": "opaque"}},
        ):
            value = copy.deepcopy(self.state)
            value.update(attack)
            application = DashboardApplication(
                lambda current=value: current,
                csrf_token="d" * 48,
            )
            with self.subTest(attack=attack):
                with self.assertRaises(ValueError):
                    application.state()

    def test_selection_errors_are_mapped_to_a_stable_public_code(self):
        def reject(*_args):
            raise dashboard.SelectionError("order_id=111111")

        paths = (
            (
                "/api/selection",
                {"selected_symbol": "US.AAPL", "target_session": "2026-08-14"},
                {"selection_callback": reject},
            ),
            (
                "/api/selection/validate",
                {"expected_sha256": "d" * 64},
                {"validation_callback": reject},
            ),
            (
                "/api/selection/arm",
                {"expected_sha256": "d" * 64, "confirmation": "ARM_NEXT_SESSION"},
                {"arming_callback": reject},
            ),
        )
        previous_handler = self.handler_class
        try:
            for path, payload, callback in paths:
                application = DashboardApplication(
                    lambda: self.state,
                    csrf_token=self.token,
                    **callback,
                )
                self.handler_class = dashboard._handler_class(application)
                status, _, body = self.request(
                    "POST",
                    path,
                    body=json.dumps(payload, separators=(",", ":")),
                    headers=self.valid_headers(),
                )
                with self.subTest(path=path):
                    self.assertEqual(status, 409, body)
                    self.assertEqual(json.loads(body)["error"], "SELECTION_CONFLICT")
        finally:
            self.handler_class = previous_handler


if __name__ == "__main__":
    unittest.main()
