"""Narrow paper-broker boundary with an optional, lazily imported moomoo SDK."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from enum import Enum
import importlib
import os
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence, Tuple
from zoneinfo import ZoneInfo

from .storage import account_fingerprint


OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111
ACCOUNT_ENV = "N1_RSI_PAPER_ACCOUNT_ID"


class BrokerError(RuntimeError):
    """Base class for broker boundary failures."""


class BrokerSafetyError(BrokerError):
    """Raised before an unsafe broker request can be sent."""


class BrokerUnavailable(BrokerError):
    """Raised when OpenD or its SDK cannot provide an authoritative response."""


class AmbiguousBrokerResponse(BrokerError):
    """Raised when an acknowledgement cannot be matched exactly to the request."""


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class LimitOrderRequest:
    intent_id: str
    symbol: str
    side: Side
    quantity: int
    limit_price: Decimal
    remark: str

    def __post_init__(self) -> None:
        if type(self.side) is not Side:
            raise TypeError("side must be an exact Side")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if type(self.remark) is not str:
            raise TypeError("remark must be an exact string")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.intent_id):
            raise ValueError("invalid intent id")
        if not re.fullmatch(r"US\.[A-Z0-9][A-Z0-9._-]{0,31}", self.symbol):
            raise ValueError("only normalized US symbols are supported")
        if not isinstance(self.limit_price, Decimal):
            object.__setattr__(self, "limit_price", Decimal(str(self.limit_price)))
        if not self.limit_price.is_finite() or self.limit_price <= 0:
            raise ValueError("limit_price must be finite and positive")
        if not self.remark.startswith("RSI1-") or self.intent_id not in self.remark:
            raise ValueError("remark must uniquely include the durable intent id")
        if len(self.remark.encode("utf-8")) > 64:
            raise ValueError("remark exceeds the broker-safe 64-byte limit")


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    filled_quantity: int
    status: str
    remark: str


@dataclass(frozen=True)
class PositionRecord:
    symbol: str
    quantity: int
    sellable_quantity: int

    def __post_init__(self) -> None:
        if type(self.quantity) is not int or self.quantity < 0:
            raise ValueError("position quantity must be a non-negative integer")
        if type(self.sellable_quantity) is not int or not (
            0 <= self.sellable_quantity <= self.quantity
        ):
            raise ValueError("sellable quantity must be between zero and quantity")


@dataclass(frozen=True)
class ReconciliationSnapshot:
    symbol: str
    orders: Tuple[OrderRecord, ...]
    position: PositionRecord


class PaperBroker(Protocol):
    """Injectable side-effect boundary used by the runner and fake tests."""

    def reconcile(self, symbol: str) -> ReconciliationSnapshot:
        ...

    def configured_account_fingerprint(self, secret_key: bytes) -> str:
        """Return a keyed label; the raw configured account never leaves the adapter."""

        ...

    def place_limit(
        self, request: LimitOrderRequest, *, selected_symbol: str
    ) -> OrderRecord:
        ...


def _environment_text(environment: Any) -> str:
    pieces = [str(environment)]
    for attribute in ("name", "value"):
        value = getattr(environment, attribute, None)
        if value is not None:
            pieces.append(str(value))
    return "|".join(pieces).upper()


def require_simulate(environment: Any) -> None:
    candidates = []
    for value in (
        environment,
        getattr(environment, "name", None),
        getattr(environment, "value", None),
    ):
        if value is None:
            continue
        text = str(value).strip().upper()
        candidates.extend((text, text.rsplit(".", 1)[-1]))
    tokens = set(candidates)
    if "REAL" in tokens or "SIMULATE" not in tokens:
        raise BrokerSafetyError("broker environment must be exactly SIMULATE")


def _default_rth_clock() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _inside_basic_rth(now: datetime) -> bool:
    if now.tzinfo is None:
        raise BrokerSafetyError("RTH clock must be timezone-aware")
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    return eastern.weekday() < 5 and time(9, 30) <= eastern.time() < time(16, 0)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AmbiguousBrokerResponse(f"invalid {field} in broker response") from exc
    if not result.is_finite():
        raise AmbiguousBrokerResponse(f"non-finite {field} in broker response")
    return result


def _integer_quantity(value: Any, field: str) -> int:
    number = _decimal(value, field)
    integral = number.to_integral_value()
    if number != integral or integral < 0:
        raise AmbiguousBrokerResponse(f"invalid {field} in broker response")
    return int(integral)


def _records(frame: Any) -> Sequence[Mapping[str, Any]]:
    if frame is None:
        raise AmbiguousBrokerResponse("broker returned no response table")
    try:
        records = frame.to_dict("records")
    except (AttributeError, TypeError, ValueError) as exc:
        raise AmbiguousBrokerResponse("broker response is not tabular") from exc
    if not isinstance(records, list) or not all(
        isinstance(row, Mapping) for row in records
    ):
        raise AmbiguousBrokerResponse("broker response rows are malformed")
    return records


def _side(value: Any) -> Side:
    text = _enum_value(value)
    if text == "BUY":
        return Side.BUY
    if text == "SELL":
        return Side.SELL
    raise AmbiguousBrokerResponse("unknown broker order side")


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().upper()


def _ack_status_is_accepted(status: str) -> bool:
    return status.strip().upper() in {
        "WAITING_SUBMIT",
        "SUBMITTING",
        "SUBMITTED",
        "FILLED_PART",
        "FILLED_ALL",
    }


def _order_from_row(row: Mapping[str, Any]) -> OrderRecord:
    order_id = str(row.get("order_id", "")).strip()
    symbol = str(row.get("code", "")).strip().upper()
    remark = str(row.get("remark", ""))
    status = str(getattr(row.get("order_status", ""), "value", row.get("order_status", ""))).upper()
    if not order_id or not symbol or not status:
        raise AmbiguousBrokerResponse("broker order identity/status is incomplete")
    quantity = _integer_quantity(row.get("qty"), "qty")
    filled = _integer_quantity(row.get("dealt_qty", 0), "dealt_qty")
    if filled > quantity:
        raise AmbiguousBrokerResponse("filled quantity exceeds requested quantity")
    return OrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=_side(row.get("trd_side")),
        quantity=quantity,
        filled_quantity=filled,
        status=status,
        remark=remark,
    )


class MoomooPaperBroker:
    """SIMULATE-only US-stock adapter.

    Importing this module is inert.  The optional SDK is imported only inside
    the first broker operation.  The adapter has no public arbitrary-call
    escape hatch and supports limit orders only.
    """

    def __init__(
        self,
        *,
        environment: Any = "SIMULATE",
        host: str = OPEND_HOST,
        port: int = OPEND_PORT,
        account_env: str = ACCOUNT_ENV,
        environ: Optional[Mapping[str, str]] = None,
        rth_clock: Callable[[], datetime] = _default_rth_clock,
        rth_verifier: Optional[Callable[[datetime], bool]] = None,
        sdk_loader: Optional[Callable[[], Any]] = None,
    ) -> None:
        require_simulate(environment)
        if host != OPEND_HOST or port != OPEND_PORT:
            raise BrokerSafetyError("OpenD endpoint must be 127.0.0.1:11111")
        source = os.environ if environ is None else environ
        account = str(source.get(account_env, "")).strip()
        if not account or not account.isdigit():
            raise BrokerSafetyError(
                f"paper account id must be supplied through {account_env}"
            )
        self._account_id = int(account)
        self._environment = "SIMULATE"
        self._rth_clock = rth_clock
        self._rth_verifier = rth_verifier
        self._sdk_loader = sdk_loader or (lambda: importlib.import_module("moomoo"))

    def configured_account_fingerprint(self, secret_key: bytes) -> str:
        """Bind activation to the same local account selected by this adapter."""

        return account_fingerprint(str(self._account_id), secret_key)

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if not re.fullmatch(r"US\.[A-Z0-9][A-Z0-9._-]{0,31}", symbol):
            raise BrokerSafetyError("broker call is restricted to one US symbol")

    def _sdk_and_context(self) -> Tuple[Any, Any]:
        try:
            sdk = self._sdk_loader()
            require_simulate(sdk.TrdEnv.SIMULATE)
            if _enum_value(sdk.TrdMarket.US) != "US":
                raise BrokerSafetyError("SDK US market constant is ambiguous")
            if _enum_value(sdk.SecurityFirm.FUTUJP) != "FUTUJP":
                raise BrokerSafetyError("SDK FUTUJP firm constant is ambiguous")
            if _enum_value(sdk.Session.RTH) != "RTH":
                raise BrokerSafetyError("SDK RTH session constant is ambiguous")
            context = sdk.OpenSecTradeContext(
                filter_trdmarket=sdk.TrdMarket.US,
                host=OPEND_HOST,
                port=OPEND_PORT,
                security_firm=sdk.SecurityFirm.FUTUJP,
            )
        except Exception as exc:
            raise BrokerUnavailable("cannot initialize local paper broker") from exc
        try:
            account_data = self._require_ok(
                sdk, context.get_acc_list(), "paper account verification"
            )
            rows = _records(account_data)
            selected = []
            for row in rows:
                try:
                    row_account_id = _integer_quantity(row.get("acc_id"), "acc_id")
                except AmbiguousBrokerResponse:
                    continue
                if row_account_id == self._account_id:
                    selected.append(row)
            if len(selected) != 1:
                raise BrokerSafetyError(
                    "selected paper account must match exactly one account-list row"
                )
            row = selected[0]
            environment = _enum_value(row.get("trd_env"))
            role = _enum_value(row.get("acc_role"))
            firm = _enum_value(row.get("security_firm"))
            status = _enum_value(row.get("acc_status"))
            authorization = row.get("trdmarket_auth")
            if not isinstance(authorization, (list, tuple, set)):
                raise BrokerSafetyError("paper account market authorization is ambiguous")
            authorized_markets = {_enum_value(item) for item in authorization}
            if environment != "SIMULATE":
                raise BrokerSafetyError("selected account is not SIMULATE")
            if role != "NORMAL":
                raise BrokerSafetyError("selected account role must be exactly NORMAL")
            if firm != "FUTUJP":
                raise BrokerSafetyError("selected account security firm is not FUTUJP")
            if status != "ACTIVE":
                raise BrokerSafetyError("selected paper account is not ACTIVE")
            if "US" not in authorized_markets:
                raise BrokerSafetyError("selected paper account lacks US authorization")
            return sdk, context
        except BrokerError:
            self._close(context)
            raise
        except Exception as exc:
            self._close(context)
            raise BrokerUnavailable("paper account verification failed") from exc

    @staticmethod
    def _close(context: Any) -> None:
        try:
            context.close()
        except Exception:
            # Closing cannot make a broker response less ambiguous; callers will
            # reconcile on the next lifecycle boundary.
            pass

    @staticmethod
    def _require_ok(sdk: Any, result: Any, operation: str) -> Any:
        if not isinstance(result, tuple) or len(result) != 2:
            raise AmbiguousBrokerResponse(f"{operation} returned an invalid envelope")
        code, data = result
        if code != sdk.RET_OK:
            raise BrokerUnavailable(f"{operation} was not accepted by OpenD")
        return data

    def _require_sellable_position(
        self, sdk: Any, context: Any, request: LimitOrderRequest
    ) -> None:
        """Re-query the exact position in the order context immediately before SELL."""

        position_data = self._require_ok(
            sdk,
            context.position_list_query(
                code=request.symbol,
                trd_env=sdk.TrdEnv.SIMULATE,
                acc_id=self._account_id,
                refresh_cache=True,
            ),
            "pre-sell position verification",
        )
        rows = _records(position_data)
        if len(rows) != 1:
            raise BrokerSafetyError(
                "SELL requires exactly one current position row"
            )
        row = rows[0]
        if str(row.get("code", "")).strip().upper() != request.symbol:
            raise BrokerSafetyError("SELL position symbol is not exact")
        if _integer_quantity(row.get("acc_id"), "position acc_id") != self._account_id:
            raise BrokerSafetyError("SELL position account is not exact")
        position_side = _enum_value(row.get("position_side"))
        if position_side != "LONG":
            raise BrokerSafetyError("SELL requires an exact LONG position")
        quantity = _integer_quantity(row.get("qty"), "position qty")
        sellable = _integer_quantity(row.get("can_sell_qty"), "position sellable qty")
        if quantity < request.quantity or sellable < request.quantity:
            raise BrokerSafetyError(
                "SELL quantity exceeds the current long/sellable quantity"
            )

    def reconcile(self, symbol: str) -> ReconciliationSnapshot:
        self._validate_symbol(symbol)
        sdk, context = self._sdk_and_context()
        try:
            environment = sdk.TrdEnv.SIMULATE
            order_data = self._require_ok(
                sdk,
                context.order_list_query(
                    code=symbol,
                    trd_env=environment,
                    acc_id=self._account_id,
                    refresh_cache=True,
                ),
                "order reconciliation",
            )
            position_data = self._require_ok(
                sdk,
                context.position_list_query(
                    code=symbol,
                    trd_env=environment,
                    acc_id=self._account_id,
                    refresh_cache=True,
                ),
                "position reconciliation",
            )
            orders = tuple(_order_from_row(row) for row in _records(order_data))
            if any(order.symbol != symbol for order in orders):
                raise AmbiguousBrokerResponse("order query returned an unrelated symbol")
            position_rows = _records(position_data)
            if len(position_rows) > 1:
                raise AmbiguousBrokerResponse(
                    "position query returned duplicate rows for one symbol/account"
                )
            quantity = 0
            sellable_quantity = 0
            if position_rows:
                row = position_rows[0]
                if str(row.get("code", "")).upper() != symbol:
                    raise AmbiguousBrokerResponse(
                        "position query returned an unrelated symbol"
                    )
                if _integer_quantity(row.get("acc_id"), "position acc_id") != self._account_id:
                    raise AmbiguousBrokerResponse(
                        "position query returned an unrelated account"
                    )
                position_side = _enum_value(row.get("position_side"))
                if position_side != "LONG":
                    raise AmbiguousBrokerResponse(
                        "position query did not prove an exact LONG position"
                    )
                quantity = _integer_quantity(row.get("qty"), "position qty")
                sellable_quantity = _integer_quantity(
                    row.get("can_sell_qty"), "position sellable qty"
                )
                if sellable_quantity > quantity:
                    raise AmbiguousBrokerResponse(
                        "sellable quantity exceeds position quantity"
                    )
            return ReconciliationSnapshot(
                symbol=symbol,
                orders=orders,
                position=PositionRecord(
                    symbol=symbol,
                    quantity=quantity,
                    sellable_quantity=sellable_quantity,
                ),
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerUnavailable("paper reconciliation failed") from exc
        finally:
            self._close(context)

    def place_limit(
        self, request: LimitOrderRequest, *, selected_symbol: str
    ) -> OrderRecord:
        # STOP-SHIP boundary: the reviewed release does not yet have a complete
        # current-session ownership ledger, durable position basis/risk anchors,
        # and fresh-process code attestation.  Keep the transport implementation
        # below for review, but make it unreachable from every public clone.
        # There is intentionally no constructor flag, environment variable, or
        # alternate public method that can widen this boundary.
        raise BrokerSafetyError(
            "SIMULATE order RPC is disabled in this reviewed release"
        )
        # Reviewed future transport remains deliberately dead code beneath the
        # unconditional stop above.  Keeping it in the same method avoids a
        # callable "private" escape hatch.
        self._validate_symbol(selected_symbol)
        if request.symbol != selected_symbol:
            raise BrokerSafetyError("request symbol differs from frozen selection")
        dispatch_time = self._rth_clock()
        if not _inside_basic_rth(dispatch_time):
            raise BrokerSafetyError("new order dispatch is outside US RTH")
        if self._rth_verifier is None:
            raise BrokerSafetyError(
                "authoritative US exchange-session verifier is required for dispatch"
            )
        try:
            verified_rth = self._rth_verifier(dispatch_time)
        except Exception as exc:
            raise BrokerSafetyError("US RTH verification failed") from exc
        if verified_rth is not True:
            raise BrokerSafetyError("current instant is not a verified US RTH session")
        sdk, context = self._sdk_and_context()
        try:
            if request.side is Side.SELL:
                # Keep this in the same context as place_order to minimize the
                # reconciliation-to-dispatch race.  Any uncertainty is a hard
                # no-submit result; no retry is attempted here.
                self._require_sellable_position(sdk, context, request)
            side = (
                sdk.TrdSide.BUY if request.side is Side.BUY else sdk.TrdSide.SELL
            )
            data = self._require_ok(
                sdk,
                context.place_order(
                    price=float(request.limit_price),
                    qty=request.quantity,
                    code=request.symbol,
                    trd_side=side,
                    order_type=sdk.OrderType.NORMAL,
                    trd_env=sdk.TrdEnv.SIMULATE,
                    acc_id=self._account_id,
                    remark=request.remark,
                    time_in_force=sdk.TimeInForce.DAY,
                    fill_outside_rth=False,
                    session=sdk.Session.RTH,
                ),
                "limit order",
            )
            rows = _records(data)
            if len(rows) != 1:
                raise AmbiguousBrokerResponse(
                    "limit order acknowledgement must contain exactly one row"
                )
            acknowledged = _order_from_row(rows[0])
            order_type = _enum_value(rows[0].get("order_type"))
            time_in_force = _enum_value(rows[0].get("time_in_force"))
            session = _enum_value(rows[0].get("session"))
            outside_rth = rows[0].get("fill_outside_rth")
            if (
                acknowledged.symbol != request.symbol
                or acknowledged.side is not request.side
                or acknowledged.quantity != request.quantity
                or acknowledged.remark != request.remark
                or _decimal(rows[0].get("price"), "price") != request.limit_price
                or order_type != "NORMAL"
                or time_in_force != "DAY"
                or session != "RTH"
                or outside_rth not in (False, 0)
                or not _ack_status_is_accepted(acknowledged.status)
            ):
                raise AmbiguousBrokerResponse(
                    "limit order acknowledgement does not exactly match intent"
                )
            return acknowledged
        except BrokerError:
            raise
        except Exception as exc:
            # Whether OpenD received the request is unknown.  The caller must
            # enter recovery and must not send the same intent again.
            raise AmbiguousBrokerResponse("limit order acknowledgement was lost") from exc
        finally:
            self._close(context)


__all__ = [
    "ACCOUNT_ENV",
    "AmbiguousBrokerResponse",
    "BrokerError",
    "BrokerSafetyError",
    "BrokerUnavailable",
    "LimitOrderRequest",
    "MoomooPaperBroker",
    "OPEND_HOST",
    "OPEND_PORT",
    "OrderRecord",
    "PaperBroker",
    "PositionRecord",
    "ReconciliationSnapshot",
    "Side",
    "require_simulate",
]
