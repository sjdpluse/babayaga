"""Identical causal fixed-window observations in training, demo and live."""
import numpy as np
from truetrade.features.technical import Candles, compute, FEATURE_NAMES

SCHEMA = list(FEATURE_NAMES)+['utc_hour_sin','utc_hour_cos','spread_fraction']
ACTIONS = ['hold','LONG','SHORT']
WINDOW = 256


def observation(rows, point):
    if len(rows)!=WINDOW:
        raise ValueError('CFD policy requires exactly 256 closed candles')
    c=Candles(**{k:[r['time' if k=='timestamp' else k] for r in rows] for k in Candles.__dataclass_fields__})
    features,atr=compute(c)
    angle=(c.timestamp[-1]%86400)/86400*2*np.pi
    spread=float(rows[-1]['spread'])*float(point)/c.close[-1]
    if not np.isfinite(spread) or spread<0:
        raise ValueError('Invalid historical spread')
    return np.r_[features[-1],np.sin(angle),np.cos(angle),spread],float(atr[-1])


def cache(rows, point):
    x=np.zeros((len(rows),len(SCHEMA))); atr=np.zeros(len(rows))
    for i in range(WINDOW-1,len(rows)):
        x[i],atr[i]=observation(rows[i-WINDOW+1:i+1],point)
    return x,atr
