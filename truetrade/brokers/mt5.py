"""Official terminal integration. All calls run on the agent's single thread.

No write retry. Entries carry SL/TP. Dedicated hedging accounts only until an
explicit netting/ownership policy exists. Order tickets are not position IDs.
"""
import hashlib
import math
import time
from truetrade.brokers.base import BrokerError, OrderRejected, OrderUncertain, Symbol, Quote
from truetrade.config import RiskLimits
from truetrade.risk.manager import Account, decimal as D
from truetrade.risk.cfd import size_signal, protection, validate_volume, validate_account


# Documented SYMBOL_FILLING_MODE bits (not exported by the Python package).
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


class MT5Broker:
    def __init__(self, settings, journal, api=None):
        self.settings, self.journal = settings, journal
        self.limits = RiskLimits.from_env()
        if api is None:
            try:
                import MetaTrader5 as api
            except ImportError:
                raise BrokerError("Install the mt5 extra on Windows with an MT5 terminal") from None
        self.api = api
        key = f"{settings.server}:{settings.login}:{settings.mode}"
        self.identity = "mt5:" + hashlib.sha256(key.encode()).hexdigest()
        self.connected = False

    def call(self, method, *args, **kwargs):
        try:
            value = getattr(self.api, method)(*args, **kwargs)
        except Exception:
            raise BrokerError("MT5 " + method + " failed") from None
        if value is None:
            raise BrokerError("MT5 " + method + " returned no result")
        return value

    async def connect(self):
        s = self.settings
        if not s.login or not s.password or not s.server or not s.terminal_path:
            raise BrokerError("MT5 login, password, exact server and terminal path required")
        if not self.call("initialize", s.terminal_path, login=s.login, password=s.password,
                         server=s.server, timeout=15000):
            raise BrokerError("MT5 initialization failed")
        self.connected = True
        self._account_info()
        self.journal.bind_broker(self.identity)

    async def shutdown(self):
        self.api.shutdown()
        self.connected = False

    def _account_info(self, trading=False):
        if not self.connected:
            raise BrokerError("MT5 not connected")
        terminal = self.call("terminal_info")
        if not terminal.connected:
            raise BrokerError("MT5 terminal disconnected")
        a = self.call("account_info")
        if a.login != self.settings.login or a.server != self.settings.server:
            raise OrderRejected("Terminal account identity changed")
        if trading:
            s, m = self.settings, self.api
            if s.mode == "paper":
                raise OrderRejected("Paper mode cannot send terminal orders")
            if s.mode == "live":
                if not s.allow_live or a.trade_mode != m.ACCOUNT_TRADE_MODE_REAL:
                    raise OrderRejected("Live requires explicit authorization and a real account")
            elif a.trade_mode != m.ACCOUNT_TRADE_MODE_DEMO:
                raise OrderRejected("Demo mode requires a verified demo account")
            if not a.trade_allowed or not a.trade_expert or not terminal.trade_allowed or terminal.tradeapi_disabled:
                raise OrderRejected("Terminal/account algorithmic trading permission denied")
            if a.margin_mode != m.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
                raise OrderRejected("Dedicated hedging account required; netting unsupported")
        return a

    async def assert_execution_allowed(self):
        self._account_info(trading=True)

    async def health(self):
        self._account_info()
        allowed = False
        try:
            await self.assert_execution_allowed()
            allowed = True
        except OrderRejected:
            pass
        return {"connected": True, "mode": self.settings.mode, "execution_allowed": allowed}

    async def resolve_symbol(self, name):
        self._account_info()
        names = {x.name for x in self.call("symbols_get")}
        explicit = self.settings.symbol_map.get(name)
        if explicit:
            if explicit not in names:
                raise OrderRejected("Configured symbol not found")
            return explicit
        if name in names:
            return name
        matches = sorted(n for n in names if n.startswith(name))
        if len(matches) != 1:
            raise OrderRejected("Symbol missing or ambiguous; configure MT5_SYMBOL_MAP")
        return matches[0]

    def _symbol(self, name):
        info = self.call("symbol_info", name)
        if not info.visible:
            if not self.call("symbol_select", name, True):
                raise OrderRejected("Symbol selection failed")
            info = self.call("symbol_info", name)
        return info

    async def symbol_info(self, name):
        name = await self.resolve_symbol(name)
        i = self._symbol(name)
        return Symbol(name, i.volume_min, i.volume_max, i.volume_step, i.trade_tick_size,
                      i.trade_tick_value, i.trade_contract_size, i.point, i.digits,
                      i.trade_stops_level, i.trade_freeze_level)

    async def quote(self, name):
        self._account_info()
        tick = self.call("symbol_info_tick", name)
        result = Quote(D(tick.bid), D(tick.ask), tick.time_msc/1000)
        result.validate()
        return result

    async def candles(self, name, timeframe="M1", count=200):
        name = await self.resolve_symbol(name)
        self._symbol(name)
        if timeframe not in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"} or type(count) is not int or not 1 <= count <= 10000:
            raise OrderRejected("Invalid candle request")
        bars = self.call("copy_rates_from_pos", name, getattr(self.api, "TIMEFRAME_"+timeframe), 1, count)
        if len(bars) != count:
            raise BrokerError("Insufficient closed-bar history")
        output = []
        for bar in bars:
            item = {k: float(bar[k]) for k in ("open", "high", "low", "close")}
            item.update(time=int(bar["time"]), volume=int(bar["tick_volume"]),
                        real_volume=int(bar["real_volume"]), spread=int(bar["spread"]))
            if not all(math.isfinite(v) for v in item.values()) or min(item[k] for k in ("open", "high", "low", "close")) <= 0:
                raise BrokerError("Invalid candle prices")
            if not item["low"] <= min(item["open"], item["close"]) <= max(item["open"], item["close"]) <= item["high"]:
                raise BrokerError("Invalid OHLC bounds")
            if min(item["volume"], item["real_volume"], item["spread"]) < 0 or item["time"] >= time.time():
                raise BrokerError("Invalid candle data")
            if output and item["time"] <= output[-1]["time"]:
                raise BrokerError("Unordered candle history")
            output.append(item)
        return output  # Tick volume is explicitly distinguished from real volume.

    def _positions(self):
        self._account_info()
        return self.call("positions_get")

    async def open_positions(self):
        return [self._position_dict(p) for p in self._positions()]

    def loss(self, side, symbol, volume, entry, stop):
        kind = self.api.ORDER_TYPE_BUY if side == "LONG" else self.api.ORDER_TYPE_SELL
        return -D(self.call("order_calc_profit", kind, symbol, float(volume), float(entry), float(stop)))

    def margin(self, side, symbol, volume, entry):
        kind = self.api.ORDER_TYPE_BUY if side == "LONG" else self.api.ORDER_TYPE_SELL
        return D(self.call("order_calc_margin", kind, symbol, float(volume), float(entry)))

    async def account(self):
        a = self._account_info()
        peak = max(D(a.equity), D(self.journal.meta("peak_equity") or a.equity))
        self.journal.set_meta("peak_equity", str(peak))
        risk, safe = D(0), True
        for p in self._positions():
            if p.magic != self.settings.magic or p.sl <= 0 or p.tp <= 0:
                safe = False
                continue
            if p.type not in {self.api.POSITION_TYPE_BUY, self.api.POSITION_TYPE_SELL}:
                safe = False
                continue
            side = "LONG" if p.type == self.api.POSITION_TYPE_BUY else "SHORT"
            info = await self.symbol_info(p.symbol)
            quote = await self.quote(p.symbol)
            mark = quote.bid if side == "LONG" else quote.ask
            sign = 1 if side == "LONG" else -1
            if sign*(mark-D(p.sl)) <= 0:
                safe = False
            stop = D(p.sl)-sign*D(self.settings.exit_slippage_points)*info.point
            fee = self.settings.commission_per_lot
            if fee is None:
                safe = False
                fee = D(0)
            risk += max(D(0), self.loss(side, p.symbol, D(p.volume), mark, stop)) + D(p.volume)*fee
        pending = self.call("orders_get")
        return Account(D(a.equity), D(a.margin_free), D(a.margin), risk, peak,
                       time.time(), safe, bool(pending))

    async def prepare(self, signal, limits):
        await self.assert_execution_allowed()
        self.limits = limits
        info = await self.symbol_info(signal.symbol)
        return size_signal(signal, info, await self.quote(info.symbol), await self.account(), limits,
                           self.loss, self.margin, self.settings.max_spread_points,
                           self.settings.deviation_points, self.settings.exit_slippage_points,
                           self.settings.commission_per_lot)

    def _filling(self, info):
        m = self.api
        # SYMBOL_FILLING flags and ORDER_FILLING enum values are distinct.
        if info.filling_mode & SYMBOL_FILLING_FOK:
            return m.ORDER_FILLING_FOK
        if info.filling_mode & SYMBOL_FILLING_IOC:
            return m.ORDER_FILLING_IOC
        if info.trade_exemode != m.SYMBOL_TRADE_EXECUTION_MARKET:
            return m.ORDER_FILLING_RETURN
        raise OrderRejected("No supported market fill policy")

    def _request(self, info, side, volume, price, *, position=None, stop=None, target=None, comment="babayaga"):
        m = self.api
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": info.name, "volume": float(volume),
               "type": m.ORDER_TYPE_BUY if side == "LONG" else m.ORDER_TYPE_SELL,
               "deviation": self.settings.deviation_points, "magic": self.settings.magic,
               "comment": comment, "type_time": m.ORDER_TIME_GTC, "type_filling": self._filling(info)}
        if info.trade_exemode != m.SYMBOL_TRADE_EXECUTION_MARKET:
            req["price"] = float(price)
        if position is not None:
            req["position"] = int(position)
        if stop is not None:
            req.update(sl=float(stop), tp=float(target))
        return req

    def _send(self, request, deadline=None):
        self._account_info(trading=True)
        check = self.call("order_check", request)
        if check.retcode != 0:
            raise OrderRejected("MT5 order_check rejected request")
        self._account_info(trading=True)
        if deadline is not None and time.time() >= deadline:
            raise OrderRejected("Signal or quote expired during order check")
        try:
            result = self.api.order_send(request)
        except BaseException:
            raise OrderUncertain("MT5 send interrupted; do not retry") from None
        if result is None:
            raise OrderUncertain("MT5 send returned no result; do not retry")
        return result

    async def open(self, plan):
        await self.assert_execution_allowed()
        if time.time()-plan.timestamp > 5 or plan.expires_at <= time.time():
            raise OrderRejected("Stale order plan")
        info = await self.symbol_info(plan.symbol)
        raw = self._symbol(plan.symbol)
        if raw.trade_mode != self.api.SYMBOL_TRADE_MODE_FULL:
            raise OrderRejected("Symbol not fully tradeable")
        quote = await self.quote(plan.symbol)
        if (quote.ask-quote.bid)/info.point > self.settings.max_spread_points:
            raise OrderRejected("Spread limit exceeded")
        validate_volume(plan.size, info)
        if protection(info, quote, plan.side, plan.stop, plan.take_profit) != (plan.stop, plan.take_profit):
            raise OrderRejected("Unnormalized protection")
        price = quote.ask if plan.side == "LONG" else quote.bid
        if abs(price-plan.entry) > D(self.settings.deviation_points)*info.point:
            raise OrderRejected("Quote moved beyond planned deviation")
        sign = 1 if plan.side == "LONG" else -1
        worst_entry = plan.entry+sign*D(self.settings.deviation_points)*info.point
        worst_stop = plan.stop-sign*D(self.settings.exit_slippage_points)*info.point
        fee = self.settings.commission_per_lot
        if fee is None:
            raise OrderRejected("Commission not reviewed")
        risk = self.loss(plan.side, plan.symbol, plan.size, worst_entry, worst_stop)+plan.size*fee
        a = await self.account()
        validate_account(a, self.limits)
        if risk <= 0 or risk > min(plan.budget, a.equity*min(plan.risk_fraction, self.limits.max_trade_risk),
                                  a.equity*self.limits.max_portfolio_risk-a.open_risk):
            raise OrderRejected("Monetary risk changed before send")
        margin = self.margin(plan.side, plan.symbol, plan.size, worst_entry)
        if margin <= 0 or margin+plan.size*fee > min(a.available_margin, a.equity*self.limits.margin_utilization-a.used_margin):
            raise OrderRejected("Insufficient margin")
        before = {p.identifier for p in self._positions()}
        comment = "by:" + hashlib.sha256(plan.decision_id.encode()).hexdigest()[:24]
        result = self._send(self._request(raw, plan.side, plan.size, price, stop=plan.stop,
                                         target=plan.take_profit, comment=comment),
                            deadline=min(plan.expires_at, plan.timestamp+5))
        receipt = {key: getattr(result, key, None) for key in ("retcode", "order", "deal", "volume", "price")}
        pid = None
        try:
            if not result.order or not result.deal:
                raise ValueError()
            deals = self.call("history_deals_get", ticket=int(result.deal))
            if len(deals) != 1:
                raise ValueError()
            deal = deals[0]
            expected = self.api.DEAL_TYPE_BUY if plan.side == "LONG" else self.api.DEAL_TYPE_SELL
            if (deal.order != result.order or deal.symbol != plan.symbol or deal.magic != self.settings.magic or
                    deal.type != expected or deal.entry != self.api.DEAL_ENTRY_IN or deal.position_id in before):
                raise ValueError()
            matches = [p for p in self._positions() if p.identifier == deal.position_id
                       and p.symbol == plan.symbol and p.magic == self.settings.magic]
            if len(matches) != 1:
                raise ValueError()
            pid = str(matches[0].ticket)
            self.journal.set_meta("position_identifier:"+pid, str(deal.position_id))
            if (result.retcode != self.api.TRADE_RETCODE_DONE or D(result.volume) != plan.size or
                    D(deal.volume) != plan.size or abs(D(result.price)-D(deal.price)) > info.trade_tick_size/2 or
                    abs(D(matches[0].price_open)-D(deal.price)) > info.trade_tick_size/2):
                raise ValueError()
        except Exception:
            raise OrderUncertain("MT5 receipt/fill not fully verified", pid, receipt) from None
        return {"positionId": pid, "receipt": receipt}

    def _position_dict(self, p):
        return {"position_id": str(p.ticket), "identifier": p.identifier, "symbol": p.symbol,
                "side": "LONG" if p.type == self.api.POSITION_TYPE_BUY else "SHORT",
                "size": D(p.volume), "entry": D(p.price_open), "stop": D(p.sl),
                "take_profit": D(p.tp), "magic": p.magic}

    async def position(self, pid):
        self._account_info()
        matches = self.call("positions_get", ticket=int(pid))
        if not matches:
            return None
        if len(matches) != 1 or str(matches[0].ticket) != str(pid):
            raise BrokerError("Ambiguous position lookup")
        return self._position_dict(matches[0])

    async def verify(self, plan, observed):
        if observed is None:
            raise BrokerError("Position not observable")
        info = await self.symbol_info(plan.symbol)
        if observed["symbol"] != plan.symbol or observed["side"] != plan.side or observed["magic"] != self.settings.magic:
            raise BrokerError("Position ownership mismatch")
        if observed["size"] != plan.size:
            raise BrokerError("Partial/unexpected fill volume")
        for key, expected in (("stop", plan.stop), ("take_profit", plan.take_profit)):
            if observed[key] <= 0 or abs(observed[key]-expected) > info.trade_tick_size/2:
                raise BrokerError("Protection not verified")
        if observed["entry"] <= 0 or abs(observed["entry"]-plan.entry) > D(self.settings.deviation_points)*info.point:
            raise BrokerError("Fill outside deviation allowance")
        sign = 1 if plan.side == "LONG" else -1
        stop = plan.stop-sign*D(self.settings.exit_slippage_points)*info.point
        fee = self.settings.commission_per_lot
        if fee is None:
            raise BrokerError("Missing commission estimate")
        risk = self.loss(plan.side, plan.symbol, plan.size, observed["entry"], stop)+plan.size*fee
        a = await self.account()
        validate_account(a, self.limits)
        if risk > min(plan.budget, a.equity*min(plan.risk_fraction, self.limits.max_trade_risk)):
            raise BrokerError("Actual fill exceeds monetary risk")
        if a.open_risk > a.equity*self.limits.max_portfolio_risk or a.used_margin > a.equity*self.limits.margin_utilization:
            raise BrokerError("Actual portfolio risk/margin exceeded")

    async def set_protection(self, pid, stop, target):
        await self.assert_execution_allowed()
        p = await self.position(pid)
        if p is None or p["magic"] != self.settings.magic:
            raise OrderRejected("Owned position required")
        if (p["stop"], p["take_profit"]) == (D(stop), D(target)):
            return
        await self.modify_position(pid, stop, target)

    async def modify_position(self, pid, stop, target):
        await self.assert_execution_allowed()
        p = await self.position(pid)
        if p is None or p["magic"] != self.settings.magic:
            raise OrderRejected("Owned position required")
        info, quote = await self.symbol_info(p["symbol"]), await self.quote(p["symbol"])
        stop, target = protection(info, quote, p["side"], stop, target, modify=True)
        if p["stop"] > 0 and ((p["side"] == "LONG" and stop < p["stop"]) or
                              (p["side"] == "SHORT" and stop > p["stop"])):
            raise OrderRejected("Stop widening forbidden")
        result = self._send({"action": self.api.TRADE_ACTION_SLTP, "position": int(pid),
                             "symbol": p["symbol"], "sl": float(stop), "tp": float(target),
                             "magic": self.settings.magic})
        observed = await self.position(pid)
        if result.retcode != self.api.TRADE_RETCODE_DONE or observed is None:
            raise OrderUncertain("Protection modification unverified", str(pid))
        if observed["stop"] != stop or observed["take_profit"] != target:
            raise OrderUncertain("Protection readback mismatch", str(pid))

    async def close(self, pid):
        await self.assert_execution_allowed()
        p = await self.position(pid)
        if p is None:
            if await self.confirm_closed(pid):
                return
            raise OrderUncertain("Position absence is not proof of close", str(pid))
        if p["magic"] != self.settings.magic:
            raise OrderRejected("Refusing to close foreign position")
        info, quote = self._symbol(p["symbol"]), await self.quote(p["symbol"])
        side = "SHORT" if p["side"] == "LONG" else "LONG"
        price = quote.bid if side == "SHORT" else quote.ask
        result = self._send(self._request(info, side, p["size"], price, position=pid, comment="babayaga close"))
        if result.retcode != self.api.TRADE_RETCODE_DONE or not result.deal or not result.order:
            raise OrderUncertain("Close result uncertain", str(pid))
        if not await self.confirm_closed(pid):
            raise OrderUncertain("Close not confirmed", str(pid))

    async def confirm_closed(self, pid):
        if await self.position(pid) is not None:
            return False
        identifier = self.journal.meta("position_identifier:"+str(pid))
        if identifier is None:
            return False
        # A changed position ticket must not look like a closure.
        if any(str(p.identifier) == identifier for p in self._positions()):
            return False
        deals = self.call("history_deals_get", position=int(identifier))
        owned = [d for d in deals if d.magic == self.settings.magic]
        incoming = sum((D(d.volume) for d in owned if d.entry == self.api.DEAL_ENTRY_IN), D(0))
        outgoing = sum((D(d.volume) for d in deals if d.entry in
                        {self.api.DEAL_ENTRY_OUT, self.api.DEAL_ENTRY_OUT_BY}), D(0))
        return incoming > 0 and outgoing == incoming
