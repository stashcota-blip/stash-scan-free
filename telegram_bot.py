"""Telegram polling once per GitHub Actions run; no always-on process."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

import requests
from scan_engine import (SYMBOLS, client, detect_signal, format_signal,
                         get_usd_news, scan_status, yahoo_1m)

LOG = logging.getLogger('stash_scan')
STATE_PATH = Path('bot_state.json')


def state_load() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        return {'last_update_id': int(data.get('last_update_id', 0)),
                'sent_keys': list(data.get('sent_keys', []))[-300:]}
    except (ValueError, OSError, TypeError):
        return {'last_update_id': 0, 'sent_keys': []}


def state_save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')


class Telegram:
    def __init__(self, token: str, session: requests.Session):
        self.url = f'https://api.telegram.org/bot{token}/'
        self.s = session

    def request(self, method: str, body: dict | None = None) -> dict:
        r = self.s.post(self.url + method, json=body or {}, timeout=20)
        r.raise_for_status()
        result = r.json()
        if not result.get('ok'):
            raise RuntimeError(f'Telegram {method} failed: {result.get("description", "unknown")}')
        return result

    def say(self, chat_id: str | int, text: str) -> None:
        for i in range(0, len(text), 3800):
            self.request('sendMessage', {'chat_id': chat_id, 'text': text[i:i + 3800],
                                         'disable_web_page_preview': True})

    def updates(self, offset: int) -> list:
        return self.request('getUpdates', {'offset': offset, 'limit': 100, 'timeout': 0,
                                           'allowed_updates': ['message']}).get('result', [])


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    group = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token:
        raise SystemExit('Missing TELEGRAM_BOT_TOKEN GitHub secret (do not paste it into chat).')
    now = datetime.now(timezone.utc)
    session = client()
    tg = Telegram(token, session)
    state = state_load()
    updates = tg.updates(state['last_update_id'] + 1)
    to_answer = []
    for u in updates:
        uid = int(u['update_id'])
        msg = u.get('message') or {}
        chat = msg.get('chat') or {}
        target = str(chat.get('id', ''))
        cmd = (msg.get('text') or '').split(' ', 1)[0].split('@')[0].lower()
        if not group:
            if cmd in ('/id', '/start') and target and chat.get('type') in ('group', 'supergroup'):
                tg.say(target, f'✅ ID skupine: {target}\nV GitHub → Secrets dodaj TELEGRAM_CHAT_ID s to številko. Nato pošlji /scan.')
        elif target == group and cmd in ('/scan', '/status', '/help', '/risk'):
            to_answer.append(cmd)
        state['last_update_id'] = max(uid, state['last_update_id'])
    if not group:
        state_save(state)
        LOG.info('Bootstrap: add bot to group and send /id.')
        return
    if group.startswith('@'):
        LOG.warning('Recommended TELEGRAM_CHAT_ID is the numeric group ID, not @name.')
    commands = to_answer[-5:]
    if '/help' in commands:
        tg.say(group, '🤖 STASH SCAN FREE\n/scan — stanje XAU/NQ (naslednji zagon do ~5+ min)\n/status — delovanje\n/risk — obvladovanje tveganja\nNima live MT5 podatkov in ne odpira pozicij.')
    if '/risk' in commands:
        tg.say(group, '🛡️ Predlog omejitve: 0.25 % računa na pozicijo, največ 2 izgubi dnevno. Brez avtomatskih lotov na futures proxy podatkih. Strategija ni backtestana.')
    if '/status' in commands:
        tg.say(group, f'🤖 STASH SCAN: OK, zagon {now:%Y-%m-%d %H:%M} UTC. GitHub Actions na ~5 min, lahko z zamudo/izpadom. Vir: Yahoo futures (lahko zamujen).')

    need_scan_report = '/scan' in commands
    news_block, news = get_usd_news(now, session)
    report = ['🔎 STASH SCAN — ' + now.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Ljubljana')).strftime('%d.%m. %H:%M') + ' SI',
              'Vir: GC=F/NQ=F futures proxy, NE brokerjev XAUUSD/NDX100.', 'Novice: ' + news]
    processed = 0
    for name, ticker in SYMBOLS.items():
        try:
            df = yahoo_1m(ticker, session, now)
            sig, why = detect_signal(name, ticker, df, now)
            report.append(scan_status(name, ticker, df, now, why))
            if sig is None:
                continue
            # Research-only alert. Proxies cannot produce executable MT5 entries.
            # Suppress alerts when news uncertain/high impact; manual /scan shows reason.
            if news_block:
                report.append(f'🛑 {ticker}: setup blokiran — {news}')
                continue
            if sig.key not in state['sent_keys']:
                tg.say(group, format_signal(sig, news, True))
                state['sent_keys'].append(sig.key)
                state['sent_keys'] = state['sent_keys'][-300:]
                processed += 1
        except Exception as e:
            LOG.exception('%s error', ticker)
            report.append(f'⚠️ {ticker}: podatki/analiza niso na voljo ({type(e).__name__})')
    if need_scan_report:
        tg.say(group, '\n'.join(report) + '\n⚠️ V zamujenih futures podatkih ni varnih MT5 entryjev.')
    state_save(state)
    LOG.info('Done; signals sent=%d commands=%s', processed, commands)


if __name__ == '__main__':
    main()
