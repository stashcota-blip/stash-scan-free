"""Transparent, educational signal scanner, not a broker execution system."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
import math
import os
import time
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger(__name__)
UTC = timezone.utc
SI = ZoneInfo('Europe/Ljubljana')
SYMBOLS = {'GOLD_FUTURES_PROXY': 'GC=F', 'NASDAQ_FUTURES_PROXY': 'NQ=F'}
PRICE_NOTE = 'Futures proxy (GC=F/NQ=F), NI cena XAUUSD/NDX100 na MT5.'


def client() -> requests.Session:
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; StashScanFree/1.0; personal research)'})
    retry = Retry(total=2, connect=2, read=2, backoff_factor=0.7,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


def yahoo_1m(ticker: str, session: requests.Session | None = None, now: datetime | None = None) -> pd.DataFrame:
    """Yahoo unofficial public chart endpoint, subject to delay/rate limits."""
    now = now or datetime.now(UTC)
    sess = session or client()
    last_err = None
    for host in ('query1.finance.yahoo.com', 'query2.finance.yahoo.com'):
        try:
            url = f'https://{host}/v8/finance/chart/{quote(ticker, safe="")}'
            r = sess.get(url, params={'interval': '1m', 'range': '7d', 'events': 'false'}, timeout=15)
            r.raise_for_status()
            body = r.json().get('chart', {})
            if body.get('error'):
                raise ValueError(f'Yahoo error: {body["error"]}')
            res = (body.get('result') or [None])[0]
            if not res:
                raise ValueError('Missing chart')
            stamps = res.get('timestamp') or []
            q = res['indicators']['quote'][0]
            if not stamps:
                raise ValueError('Missing timestamps')
            df = pd.DataFrame({'open': q['open'], 'high': q['high'],
                               'low': q['low'], 'close': q['close']},
                              index=pd.to_datetime(stamps, unit='s', utc=True))
            df = df.apply(pd.to_numeric, errors='coerce').dropna()
            df = df.loc[~df.index.duplicated(keep='last')].sort_index()
            # Reject the still-forming one-minute candle, never use future candles.
            df = df[df.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
            if len(df) < 300:
                raise ValueError(f'Only {len(df)} full M1 candles')
            return df
        except Exception as e:
            last_err = e
            LOG.warning('%s source error: %s', host, e)
    raise RuntimeError(f'Data unavailable for {ticker}: {last_err}')


def resample_closed(df: pd.DataFrame, freq: str, now: datetime) -> pd.DataFrame:
    """UTC-aligned timeframes; filters the forming period."""
    bars = df.resample(freq, label='left', closed='left').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    period = pd.Timedelta(freq)
    return bars[bars.index + period <= pd.Timestamp(now)]


def atr(df: pd.DataFrame, length=14) -> pd.Series:
    prev = df.close.shift(1)
    true_range = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def ema_bias(df: pd.DataFrame) -> str:
    if len(df) < 58:
        return 'NEUTRAL'
    e20 = df.close.ewm(span=20, adjust=False).mean()
    e50 = df.close.ewm(span=50, adjust=False).mean()
    rising = e20.iloc[-1] > e20.iloc[-4] and e50.iloc[-1] > e50.iloc[-4]
    falling = e20.iloc[-1] < e20.iloc[-4] and e50.iloc[-1] < e50.iloc[-4]
    if df.close.iloc[-1] > e20.iloc[-1] > e50.iloc[-1] and rising:
        return 'BUY'
    if df.close.iloc[-1] < e20.iloc[-1] < e50.iloc[-1] and falling:
        return 'SELL'
    return 'NEUTRAL'


@dataclass(frozen=True)
class Signal:
    name: str
    ticker: str
    direction: str
    candle_at: datetime
    sweep_at: datetime
    entry: float
    stop: float
    tp1: float
    tp2: float
    score: int
    bias_h1: str
    bias_m30: str
    reason: str
    source_age_min: float
    news_status: str = ''

    @property
    def key(self) -> str:
        return f'{self.ticker}:{self.direction}:{self.candle_at.isoformat()}:{self.sweep_at.isoformat()}'


def find_sweep(m15: pd.DataFrame, direction: str, num=12, freshness=6):
    """M15 sweep and close back inside the previous 12 bars, not lookahead."""
    if len(m15) < num + freshness + 2:
        return None
    a = atr(m15).fillna(0)
    for i in range(len(m15) - 1, max(num, len(m15) - freshness - 1), -1):
        candle = m15.iloc[i]
        previous = m15.iloc[i - num:i]
        pad = max(0.02 * float(a.iloc[i]), 0.000001)
        if direction == 'BUY':
            lev = float(previous.low.min())
            valid = candle.low < lev - pad and candle.close > lev and candle.close > candle.open
        else:
            lev = float(previous.high.max())
            valid = candle.high > lev + pad and candle.close < lev and candle.close < candle.open
        if valid:
            return m15.index[i], float(candle.low if direction == 'BUY' else candle.high), lev
    return None


def find_bos(m5: pd.DataFrame, direction: str, after: datetime, lookback=5):
    """First M5 close breaking pre-existing 5-bar high/low AFTER M15 sweep closes."""
    if len(m5) < lookback + 2:
        return None
    for i in range(lookback, len(m5)):
        ts = m5.index[i]
        if ts < pd.Timestamp(after) + pd.Timedelta(minutes=15):
            continue
        if ts > pd.Timestamp(after) + pd.Timedelta(minutes=50):
            break
        previous = m5.iloc[i - lookback:i]
        boundary = float(previous.high.max() if direction == 'BUY' else previous.low.min())
        c = m5.iloc[i]
        if direction == 'BUY' and c.close > boundary and c.close > c.open:
            return ts, boundary
        if direction == 'SELL' and c.close < boundary and c.close < c.open:
            return ts, boundary
    return None


def detect_signal(name: str, ticker: str, m1: pd.DataFrame, now: datetime) -> tuple[Optional[Signal], str]:
    """Only closed bars. Data freshness determines whether this is useful as live info."""
    if m1.empty or len(m1) < 400:
        return None, 'premalo sveč'
    age = (pd.Timestamp(now) - (m1.index[-1] + pd.Timedelta(minutes=1))).total_seconds() / 60
    if age < -0.1:
        return None, 'nepravilna ura podatkov'
    if age > 20:
        return None, f'zastareli podatki ({age:.0f} min)'
    m5 = resample_closed(m1, '5min', now)
    m15 = resample_closed(m1, '15min', now)
    m30 = resample_closed(m1, '30min', now)
    h1 = resample_closed(m1, '1h', now)
    if min(len(m5), len(m15), len(m30), len(h1)) < 58:
        return None, 'premalo zgodovine za vse timeframe'
    h1_dir = ema_bias(h1)
    m30_dir = ema_bias(m30)
    if h1_dir == 'NEUTRAL' or h1_dir != m30_dir:
        return None, f'ni soglasja H1={h1_dir}, M30={m30_dir}'
    d = h1_dir
    sw = find_sweep(m15, d)
    if sw is None:
        return None, f'ni svežega M15 liquidity sweep ({d})'
    sweep_ts, extreme, _ = sw
    bos = find_bos(m5, d, sweep_ts)
    if bos is None:
        return None, 'ni potrjenega M5 break of structure'
    bos_ts, level = bos
    # One M1 close, no retrospective price guessing: use last fully closed M1 bar.
    candle = m1.iloc[-1]
    stamp = m1.index[-1]
    if stamp < bos_ts + pd.Timedelta(minutes=5):
        return None, 'čakamo M1 retest za BOS'
    if stamp > bos_ts + pd.Timedelta(minutes=25):
        return None, 'M5 BOS je prestar'
    a = float(atr(m5).iloc[-1])
    if not math.isfinite(a) or a <= 0:
        return None, 'ATR ni na voljo'
    pad = max(a * 0.2, 0.01)
    if d == 'BUY':
        confirmed = candle.low <= level + pad and candle.close > level and candle.close > candle.open
    else:
        confirmed = candle.high >= level - pad and candle.close < level and candle.close < candle.open
    if not confirmed:
        return None, 'čakamo bullish/bearish M1 retest'
    entry = float(candle.close)
    # Stop based on structural M5 displacement & sweep, bounded to avoid oversized stop.
    recent = m5[m5.index >= pd.Timestamp(bos_ts) - pd.Timedelta(minutes=15)]
    if d == 'BUY':
        stop = min(float(recent.low.min()), extreme) - 0.1 * a
        risk = entry - stop
        nearest = float(m15.high.tail(16).max())
        if nearest <= entry + 1.5 * risk:
            return None, 'premalo prostora do M15 upora'
        tp1, tp2 = entry + 1.5 * risk, entry + 2.0 * risk
    else:
        stop = max(float(recent.high.max()), extreme) + 0.1 * a
        risk = stop - entry
        nearest = float(m15.low.tail(16).min())
        if nearest >= entry - 1.5 * risk:
            return None, 'premalo prostora do M15 podpore'
        tp1, tp2 = entry - 1.5 * risk, entry - 2.0 * risk
    if risk <= 0 or risk > 2.5 * a:
        return None, f'neprimerna razdalja SL ({risk:.2f})'
    score = 4 + int(age <= 3)
    return Signal(name, ticker, d, stamp.to_pydatetime(), sweep_ts.to_pydatetime(),
                  entry, stop, tp1, tp2, score, h1_dir, m30_dir,
                  'H1/M30 trend + M15 sweep + M5 BOS + M1 retest', round(age, 1)), 'setup'


def get_usd_news(now: datetime, session: requests.Session) -> tuple[bool, str]:
    """High-impact USD ForexFactory public mirror; fail closed at NY macro hours if unavailable."""
    try:
        r = session.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=10)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            raise ValueError('invalid calendar format')
        for item in items:
            if item.get('country') != 'USD' or str(item.get('impact', '')).lower() != 'high':
                continue
            raw = str(item.get('date', ''))
            when = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if when.tzinfo is None:
                raise ValueError('calendar event without timezone')
            delta = (now - when.astimezone(UTC)).total_seconds() / 60
            if -30 <= delta <= 30:
                return True, f'High-impact USD: {item.get("title", "news")} ({when.astimezone(SI):%H:%M} SI)'
        return False, 'Koledar preverjen (brez High USD v ±30 min)'
    except Exception as exc:
        LOG.warning('News calendar unavailable: %s', exc)
        if 12 <= now.hour < 18 and now.weekday() < 5:
            return True, 'Koledar ni na voljo: konzervativno brez setupov 12–18 UTC'
        return False, 'Koledar ni na voljo (preveri novice ročno)'


def format_signal(s: Signal, news: str, allowed: bool) -> str:
    time_si = s.candle_at.astimezone(SI).strftime('%d.%m. %H:%M')
    prefix = '🛑 BLOKIRAN TESTNI SETUP' if not allowed else '🔬 TESTNI SIGNAL (NE ZA IZVEDBO NA MT5)'
    fmt = lambda x: f'{x:,.2f}'
    return (f'{prefix}\n{s.name} / {s.ticker} {s.direction}\n'
            f'Vir: {PRICE_NOTE}\n'
            f'Sveča: {time_si} SI | vir zamuja ~{s.source_age_min:.0f} min\n'
            f'FUTURES PROXY Entry: {fmt(s.entry)}\n'
            f'FUTURES PROXY SL: {fmt(s.stop)}\n'
            f'FUTURES PROXY TP1: {fmt(s.tp1)} | TP2: {fmt(s.tp2)}\n'
            f'Model: {s.reason}\nH1={s.bias_h1}, M30={s.bias_m30}, kakovost {s.score}/5\n'
            f'Novice: {news}\n'
            '⚠️ Podatki so lahko zamaknjeni. To NISO cene tvojega brokerja. '
            'Ne kopiraj teh entry/SL/TP nivojev v MT5. Pred dejanskim trejdom je potreben live bid/ask in test strategije.')


def scan_status(name: str, ticker: str, df: pd.DataFrame, now: datetime, note: str) -> str:
    age = (pd.Timestamp(now) - df.index[-1] - pd.Timedelta(minutes=1)).total_seconds() / 60
    return f'{name} ({ticker}): {note}; zadnja zaprta M1 {df.index[-1].astimezone(SI):%H:%M} SI; zamik ~{age:.0f} min'
