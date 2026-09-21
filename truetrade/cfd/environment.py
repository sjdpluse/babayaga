"""Entry-decision CFD simulator using actual lot/tick metadata and shared sizing.

Positions run until SL/TP. Each action advances to the next flat decision point;
PPO discounts decision steps, not wall-clock bars. Bid OHLC/ask spread is modeled;
bar ambiguity resolves stop-first. No crypto funding or liquidation formula.
Historical margin, swap and slippage are reviewed approximations, not tick replay.
"""
from datetime import datetime, timezone
import time
import numpy as np
from truetrade.brokers.base import Symbol, Quote, Signal
from truetrade.config import RiskLimits
from truetrade.risk.cfd import size_signal
from truetrade.risk.manager import Account, decimal as D, RiskRejected
from truetrade.brokers.base import OrderRejected
from truetrade.cfd.observations import WINDOW


class CFDEnv:
    def __init__(self,rows,contract,features,atr,norm,start,end,*,risk=.005,stress=1.,random_start=True,episode_decisions=256):
        if not WINDOW-1<=start<end<len(rows) or stress<1 or not 0<risk<=.005:
            raise ValueError('Invalid CFD episode')
        self.rows,self.contract,self.features,self.atr,self.norm=rows,contract,features,atr,norm
        self.start,self.end,self.risk,self.stress=start,end,risk,stress
        self.random_start,self.episode_decisions=random_start,episode_decisions
        self.symbol=Symbol(**contract.symbol)
        self.rng=np.random.default_rng(0)
        self.reset()

    def reset(self,seed=None,options=None):
        if seed is not None:self.rng=np.random.default_rng(seed)
        self.i=int(self.rng.integers(self.start,max(self.start+1,self.end-100))) if self.random_start else self.start
        self.balance=self.peak=10000.
        self.closed=[]; self.equity_curve=[10000.]; self.decisions=0; self.done=False
        return self.norm.transform(self.features[self.i]),{}

    def carry(self, start, end, side, lots):
        c=self.contract
        cost=c.swap_long_cost if side=='LONG' else c.swap_short_cost
        total=0.
        for day in range(int(start//86400),int(end//86400)+1):
            roll=day*86400+c.rollover_utc_hour*3600
            weekday=datetime.fromtimestamp(roll,timezone.utc).weekday()
            if start<roll<=end and weekday<5:
                total += cost*lots*(3 if weekday==c.triple_weekday else 1)*self.stress
        return total

    def step(self, action, confidence=1.):
        if self.done or not isinstance(action,(int,np.integer)) or isinstance(action,bool) or not 0<=action<3:
            raise ValueError('Invalid CFD action/state')
        old=self.balance; old_dd=1-old/self.peak
        previous=self.i; self.i+=1; self.decisions+=1
        r=self.rows[self.i]; c=self.contract; s=self.symbol
        seconds=self.rows[previous]['_seconds']
        # A signal would expire across a session gap; do not fill one there.
        if action and r['time']-self.rows[previous]['time']==seconds:
            side='LONG' if action==1 else 'SHORT'; sign=1 if action==1 else -1
            spread=float(r['spread'])*float(s.point)*self.stress
            bid=float(r['open']); ask=bid+spread; quote=Quote(D(bid),D(ask),time.time())
            planned=ask if action==1 else bid
            distance=2*self.atr[previous]
            sig=Signal('simulation',s.symbol,side,D(planned-sign*distance),D(planned+sign*distance*2),
                       D(self.risk),time.time(),time.time()+60)
            account=Account(D(old),D(old),D(0),D(0),D(self.peak),time.time())
            try:
                plan=size_signal(sig,s,quote,account,RiskLimits(max_trade_risk=D(self.risk)),
                    lambda side,sym,lot,entry,stop: D(sign)*lot*(entry-stop)*D(c.value_per_price_lot),
                    lambda side,sym,lot,entry: lot*entry*D(c.margin_rate),D(c.max_spread_points),
                    c.entry_slippage_points*self.stress,c.exit_slippage_points*self.stress,c.commission*self.stress)
            except (RiskRejected,OrderRejected):
                plan=None
            if plan:
                lots=float(plan.size); entry=planned+sign*c.entry_slippage_points*float(s.point)*self.stress
                fee=lots*c.commission*self.stress; stop=float(plan.stop); target=float(plan.take_profit)
                entered=r['time']; exit_price=None; reason='end_of_evaluation'
                while self.i<=self.end:
                    b=self.rows[self.i]; sp=float(b['spread'])*float(s.point)*self.stress
                    # Long liquidates at Bid; short at Ask.
                    o,h,l,mark=[float(b[k])+(sp if sign<0 else 0) for k in ('open','high','low','close')]
                    if sign*(o-stop)<=0:exit_price=o;reason='gap_stop'
                    elif sign*(o-target)>=0:exit_price=target;reason='gap_target'
                    elif (l<=stop if sign>0 else h>=stop):exit_price=stop;reason='stop'
                    elif (h>=target if sign>0 else l<=target):exit_price=target;reason='target'
                    swaps=self.carry(entered,b['time']+seconds,side,lots)
                    if exit_price is not None or self.i==self.end:
                        exit_price=mark if exit_price is None else exit_price
                        # Adverse exit allowance on all fills, even limit targets, conservatively.
                        exit_price-=sign*c.exit_slippage_points*float(s.point)*self.stress
                        pnl=sign*(exit_price-entry)*lots*c.value_per_price_lot-fee-swaps
                        self.balance=old+pnl; self.equity_curve.append(self.balance)
                        self.closed.append({'net_pnl':pnl,'risk':float(plan.risk),'r_multiple':pnl/float(plan.risk),
                            'entry':entry,'exit':exit_price,'volume':lots,'side':side,'reason':reason,
                            'opened_at':entered,'closed_at':b['time']+seconds,'fees':fee,'swap':swaps})
                        break
                    marked=old+sign*(mark-entry)*lots*c.value_per_price_lot-fee-swaps
                    self.equity_curve.append(marked); self.i+=1
        self.peak=max(self.peak,max(self.equity_curve))
        self.equity_curve.append(self.balance)
        terminated=self.balance<=0 or 1-self.balance/self.peak>=.15
        truncated=self.i>=self.end or (self.random_start and self.decisions>=self.episode_decisions)
        self.done=terminated or truncated
        reward=100*((self.balance-old)/max(old,1)-.1*max(0,1-self.balance/self.peak-old_dd))
        return self.norm.transform(self.features[min(self.i,self.end)]),float(reward),terminated,truncated,{}
