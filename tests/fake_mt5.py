"""Deterministic MT5 contract fixture. Never imports the vendor package."""
import time
from types import SimpleNamespace as NS


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ORDER_TYPE_BUY = POSITION_TYPE_BUY = DEAL_TYPE_BUY = 0
    ORDER_TYPE_SELL = POSITION_TYPE_SELL = DEAL_TYPE_SELL = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_GTC = 0
    SYMBOL_TRADE_EXECUTION_MARKET = 2
    SYMBOL_TRADE_MODE_FULL = 4
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3
    TIMEFRAME_M1 = 1

    def __init__(self):
        self.account_value = NS(login=12345, server="Fixture-Demo", trade_mode=0, margin_mode=2,
            trade_allowed=True, trade_expert=True, equity=10000, margin=0, margin_free=10000, currency="USD")
        self.terminal_value = NS(connected=True, trade_allowed=True, tradeapi_disabled=False)
        self.info = NS(name="XAUUSD", visible=True, volume_min=.01, volume_max=100., volume_step=.01,
            trade_tick_size=.01, trade_tick_value=1., trade_contract_size=100., point=.01, digits=2,
            trade_stops_level=20, trade_freeze_level=10, trade_mode=4, filling_mode=3, trade_exemode=2)
        self.names = ["XAUUSD"]
        self.bid, self.ask = 2000., 2000.2
        self.tick_age = 0
        self.positions, self.deals, self.orders = {}, {}, {}
        self.requests, self.checks = [], []
        self.check_code, self.seq = 0, 0
        self.check_none = False
        self.check_error = None
        self.send_code = None
        self.last_error_value = (1, "Success")
        self.last_error_reads = 0
        self.partial = self.lose_response = self.broken_protection = False
        self.fill_slippage = 0
        self.pending = ()
        self.fail_positions = self.fail_profit = self.fail_margin = self.fail_init = self.fail_send = False

    def initialize(self, *args, **kwargs): return not self.fail_init
    def shutdown(self): pass
    def account_info(self): return self.account_value
    def terminal_info(self): return self.terminal_value
    def symbols_get(self): return tuple(NS(name=n) for n in self.names)
    def symbol_info(self, name):
        return NS(**{**vars(self.info), "name": name}) if name in self.names else None
    def symbol_select(self, *args): return True
    def symbol_info_tick(self, name):
        return NS(bid=self.bid, ask=self.ask, time_msc=int((time.time()-self.tick_age)*1000))
    def positions_get(self, ticket=None):
        if self.fail_positions: return None
        return tuple(p for p in self.positions.values() if ticket is None or p.ticket == ticket)
    def orders_get(self): return self.pending
    def order_calc_profit(self, action, symbol, volume, entry, stop):
        if self.fail_profit: return None
        return round((1 if action == 0 else -1)*(stop-entry)*100*volume, 8)
    def order_calc_margin(self, action, symbol, volume, price):
        return None if self.fail_margin else 2000*volume
    def order_check(self, req):
        self.checks.append(req.copy())
        if self.check_error is not None: raise self.check_error
        if self.check_none: return None
        return NS(retcode=self.check_code)

    def last_error(self):
        self.last_error_reads += 1
        return self.last_error_value

    def order_send(self, req):
        self.requests.append(req.copy())
        if self.fail_send: raise TimeoutError("fixture timeout")
        if self.send_code is not None:
            return NS(retcode=self.send_code, order=0, deal=0, volume=0., price=0.)
        self.seq += 1
        order, deal_id = 100+self.seq, 1000+self.seq
        volume = req.get("volume", 0)
        if req["action"] == self.TRADE_ACTION_SLTP:
            p = self.positions[req["position"]]
            if not self.broken_protection: p.sl, p.tp = req["sl"], req["tp"]
            return NS(retcode=self.TRADE_RETCODE_DONE, deal=0, order=0, volume=0, price=0)
        if "position" in req:
            p = self.positions.pop(req["position"])
            price = self.bid if req["type"] == 1 else self.ask
            position_identifier = p.identifier
            self.deals[deal_id] = NS(ticket=deal_id, order=order, position_id=position_identifier,
                symbol=p.symbol, magic=req["magic"], type=req["type"], entry=1, volume=volume, price=price)
            self.account_value.margin = round(self.account_value.margin-volume*2000, 8)
        else:
            if self.partial: volume /= 2
            price = (self.ask if req["type"] == 0 else self.bid) + self.fill_slippage
            ticket, identifier = 20000+order, 30000+order
            position_identifier = identifier
            self.positions[ticket] = NS(ticket=ticket, identifier=identifier, symbol=req["symbol"],
                type=req["type"], volume=volume, price_open=price,
                sl=0 if self.broken_protection else req["sl"],
                tp=0 if self.broken_protection else req["tp"], magic=req["magic"])
            self.deals[deal_id] = NS(ticket=deal_id, order=order, position_id=identifier,
                symbol=req["symbol"], magic=req["magic"], type=req["type"], entry=0, volume=volume, price=price)
            self.account_value.margin += volume*2000

        self.orders[order] = NS(
            ticket=order,
            position_id=position_identifier,
            symbol=req["symbol"],
            magic=req["magic"],
            type=req["type"],
        )
        if self.lose_response: return None
        return NS(retcode=10010 if self.partial and "position" not in req else 10009,
                  deal=deal_id, order=order, volume=volume, price=price)

    def history_orders_get(self, ticket=None, position=None):
        return tuple(o for o in self.orders.values()
                     if (ticket is None or o.ticket == ticket)
                     and (position is None or o.position_id == position))

    def history_deals_get(self, ticket=None, position=None):
        return tuple(d for d in self.deals.values()
                     if (ticket is None or d.ticket == ticket)
                     and (position is None or d.position_id == position))

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        assert start == 1
        now = int(time.time())//60*60
        return [dict(time=now-(count-i)*60, open=2000., high=2001., low=1999., close=2000.2,
                     tick_volume=10, real_volume=0, spread=20) for i in range(count)]
