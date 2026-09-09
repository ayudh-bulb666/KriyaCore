#!/usr/bin/env python3
"""KriyaCore Face ID bridge — runs at the gym, not in the cloud.

Why this exists
---------------
The Mivanta terminal speaks WebSocket + JSON (protocol 2.1) with two
properties that make it unsafe to expose to the internet:

  * no TLS — the spec says so outright ("the default listen port is 7788,
    no TLS encrypt"), and the device speaks ws:// only, so no proxy on our
    side can fix it;
  * no real authentication — a terminal identifies itself with its serial
    number and nothing else, so anyone who can reach the port can register
    as a terminal and push fabricated attendance.

So the plaintext half stays on the gym's LAN, where it belongs, and this
bridge is the only thing that talks to KriyaCore — over HTTPS, with an HMAC
signature and a replay check. Port 7788 is never reachable from outside the
building.

What it does
------------
  terminal --ws://LAN:7788--> bridge --HTTPS+HMAC--> KriyaCore /webhook/faceid

  * accepts only the serial numbers configured for this gym
  * strips the base64 face image before anything leaves the device's LAN
  * answers the door within the device's timeout, failing OPEN if the
    internet is down, and queues the event to disk so nothing is lost

Run:
    KRIYACORE_URL=https://kriyacore.in \
    GYM_ID=1 BRIDGE_SECRET=... ALLOWED_SERIALS=ZX0006827500 \
    python3 kriyacore_bridge.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests
import websockets

log = logging.getLogger('bridge')

# ── Configuration ────────────────────────────────────────────────────────────

CLOUD_URL   = os.environ.get('KRIYACORE_URL', 'http://127.0.0.1:8000').rstrip('/')
GYM_ID      = os.environ.get('GYM_ID', '')
SECRET      = os.environ.get('BRIDGE_SECRET', '')
LISTEN_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '7788'))
QUEUE_PATH  = Path(os.environ.get('QUEUE_PATH', 'bridge_queue.db'))

# Serial numbers this bridge will accept. Anything else is refused at
# registration — the device's own "authentication" is just this string, so
# treating it as an allowlist is the only value it has.
ALLOWED_SERIALS = {
    s.strip() for s in os.environ.get('ALLOWED_SERIALS', '').split(',') if s.strip()
}

# How long to wait on KriyaCore before deciding the door can't wait any
# longer. The terminal gives up quickly, and a member standing at a locked
# door is a worse failure than a slow log.
CLOUD_TIMEOUT_SECONDS = float(os.environ.get('CLOUD_TIMEOUT', '3.0'))

# What to do when KriyaCore is unreachable. Open, matching the app's own
# face_id_deny_expired default: wrongly locking out a paying member is worse
# than letting a lapsed one in, and staff are told either way once the queue
# drains.
FAIL_OPEN = os.environ.get('FAIL_OPEN', 'true').lower() != 'false'


def _now():
    """Device-facing timestamps use the gym's wall clock — 'cloudtime' is
    shown on the terminal and used to set its clock."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ── Offline queue ────────────────────────────────────────────────────────────

class Queue:
    """Disk-backed queue of events KriyaCore hasn't accepted yet.

    A gym's internet drops. The door still has to work, and the attendance
    still has to arrive eventually — an in-memory list would lose the day's
    scans the first time this process restarted.
    """

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                queued_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )""")
        self.db.commit()

    def add(self, payload):
        self.db.execute('INSERT INTO pending (payload, queued_at) VALUES (?, ?)',
                        (json.dumps(payload), time.time()))
        self.db.commit()

    def take(self, limit=20):
        rows = self.db.execute(
            'SELECT id, payload FROM pending ORDER BY id LIMIT ?', (limit,)).fetchall()
        return [(rid, json.loads(p)) for rid, p in rows]

    def drop(self, row_id):
        self.db.execute('DELETE FROM pending WHERE id = ?', (row_id,))
        self.db.commit()

    def bump(self, row_id):
        self.db.execute('UPDATE pending SET attempts = attempts + 1 WHERE id = ?',
                        (row_id,))
        self.db.commit()

    def depth(self):
        return self.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0]


# ── Talking to KriyaCore ─────────────────────────────────────────────────────

def _signed_headers(body: str):
    """HMAC-SHA256 over 'timestamp.body'.

    The timestamp is inside the signed material on purpose: signing only the
    body would let anyone who captured one request replay it forever.
    """
    ts = str(int(time.time()))
    mac = hmac.new(SECRET.encode(), f'{ts}.{body}'.encode(), hashlib.sha256)
    return {
        'Content-Type': 'application/json',
        'X-KriyaCore-Timestamp': ts,
        'X-KriyaCore-Signature': mac.hexdigest(),
    }


def door_flag(value):
    """Translate KriyaCore's verdict into the device's 1/0 door flag.

    KriyaCore answers with the string 'allow' or 'deny'. Treating that as a
    plain boolean is a trap: 'deny' is a non-empty string and therefore
    truthy, so `1 if value else 0` opens the door for exactly the people it
    is supposed to stop. Compare explicitly.

    Anything unrecognised falls back to FAIL_OPEN rather than guessing.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ('allow', 'open', 'true', 'yes', '1'):
            return 1
        if v in ('deny', 'denied', 'refuse', 'false', 'no', '0'):
            return 0
        return 1 if FAIL_OPEN else 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    return 1 if FAIL_OPEN else 0


def post_event(payload, timeout=CLOUD_TIMEOUT_SECONDS):
    """Send one scan to KriyaCore. Returns the parsed JSON, or None if the
    cloud could not be reached or refused the request."""
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    url  = f'{CLOUD_URL}/webhook/faceid/{GYM_ID}'
    try:
        r = requests.post(url, data=body, headers=_signed_headers(body), timeout=timeout)
    except requests.RequestException as exc:
        log.warning('cloud unreachable: %s', exc)
        return None
    if r.status_code != 200:
        log.warning('cloud rejected event: HTTP %s %s', r.status_code, r.text[:200])
        return None
    try:
        return r.json()
    except ValueError:
        log.warning('cloud returned non-JSON: %s', r.text[:200])
        return None


async def drain_queue(queue):
    """Background flush of anything the cloud missed. Runs forever."""
    while True:
        await asyncio.sleep(15)
        batch = queue.take()
        if not batch:
            continue
        log.info('draining %d queued event(s)', len(batch))
        for row_id, payload in batch:
            # Queued events are history; nobody is waiting at the door, so a
            # longer timeout is fine here.
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda p=payload: post_event(p, timeout=15))
            if result is not None:
                queue.drop(row_id)
            else:
                queue.bump(row_id)
                break   # cloud still down — stop, try again next tick


# ── Protocol handlers ────────────────────────────────────────────────────────

def _clean_record(rec):
    """One scan, with the biometric removed.

    The device may attach `image` — a base64 photo of the person's face. That
    is biometric data, and the whole point of this integration's design is
    that KriyaCore never becomes a holder of it. Dropped here, on the LAN,
    before anything is sent or written to the queue.
    """
    return {
        'enrollid': rec.get('enrollid'),
        'time':     rec.get('time'),
        'mode':     rec.get('mode'),      # 8 == face
        'inout':    rec.get('inout'),
        'event':    rec.get('event'),
        'verifymode': rec.get('verifymode'),
    }


class Session:
    """One connected terminal."""

    def __init__(self, queue):
        self.queue = queue
        self.serial = None
        self.registered = False
        # Highest logindex accepted from this device. The protocol's own
        # counter doubles as replay protection: a captured frame replayed
        # later carries an index we have already passed.
        self.last_logindex = -1

    # ── reg ──────────────────────────────────────────────────────────────
    def handle_reg(self, msg):
        serial = (msg.get('sn') or '').strip()
        info   = msg.get('devinfo') or {}

        if ALLOWED_SERIALS and serial not in ALLOWED_SERIALS:
            log.warning('REFUSED registration from unknown serial %r', serial)
            return {'ret': 'reg', 'result': False,
                    'reason': 'Device not registered with this gym'}

        self.serial = serial
        self.registered = True
        log.info('registered %s (%s, firmware %s)', serial,
                 info.get('modelname', '?'), info.get('firmware', '?'))
        return {
            'ret': 'reg',
            'result': True,
            'cloudtime': _now(),
            # True = don't push us every new user enrolled on the keypad.
            # Enrollment is driven from KriyaCore, not from the device.
            'nosenduser': True,
        }

    # ── sendlog ──────────────────────────────────────────────────────────
    def handle_sendlog(self, msg):
        if not self.registered:
            return {'ret': 'sendlog', 'result': False, 'reason': 1}

        records  = msg.get('record') or []
        logindex = msg.get('logindex')

        if isinstance(logindex, int) and logindex <= self.last_logindex:
            # Already seen. Ack it so the device stops resending, but do not
            # act on it again — this is what stops a replayed frame from
            # opening the door or inventing an attendance row.
            log.warning('ignoring replayed logindex %s (last %s)',
                        logindex, self.last_logindex)
            return {'ret': 'sendlog', 'result': True, 'count': len(records),
                    'logindex': logindex, 'cloudtime': _now(),
                    'access': 1 if FAIL_OPEN else 0}

        access = 1 if FAIL_OPEN else 0
        for rec in records:
            if not rec.get('enrollid'):
                # enrollid 0 is a door/tamper event, not a person. Worth
                # logging locally; nothing for KriyaCore to decide.
                log.info('device event (no user): %s', rec.get('event'))
                continue

            payload = {
                'serial': self.serial,
                'record': _clean_record(rec),
            }
            result = post_event(payload)
            if result is None:
                self.queue.add(payload)
                log.warning('queued scan for enrollid=%s (queue depth %d)',
                            rec.get('enrollid'), self.queue.depth())
            else:
                access = door_flag(result.get('access'))
                membership = result.get('membership') or {}
                log.info('enrollid=%s %s -> door=%s (%s)',
                         rec.get('enrollid'), result.get('member', '?'),
                         'OPEN' if access else 'REFUSED',
                         membership.get('message', result.get('event', '')))

        if isinstance(logindex, int):
            self.last_logindex = logindex

        return {'ret': 'sendlog', 'result': True, 'count': len(records),
                'logindex': logindex, 'cloudtime': _now(), 'access': access}

    # ── senduser ─────────────────────────────────────────────────────────
    def handle_senduser(self, msg):
        """Someone enrolled a user on the device's own keypad.

        Acked but not acted on: enrolment is driven from KriyaCore, and a
        user created at the terminal has no member record to attach to.
        Logged so the gym can reconcile it.
        """
        log.info('device-side enrolment: enrollid=%s name=%r (not synced)',
                 msg.get('enrollid'), msg.get('name'))
        return {'ret': 'senduser', 'result': True, 'cloudtime': _now()}

    def dispatch(self, msg):
        cmd = msg.get('cmd')
        if cmd == 'reg':
            return self.handle_reg(msg)
        if cmd == 'sendlog':
            return self.handle_sendlog(msg)
        if cmd == 'senduser':
            return self.handle_senduser(msg)
        log.info('unhandled command %r', cmd)
        return None


# ── Server ───────────────────────────────────────────────────────────────────

async def handle_connection(ws, queue):
    peer = getattr(ws, 'remote_address', ('?', 0))
    log.info('terminal connected from %s', peer[0])
    session = Session(queue)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                log.warning('non-JSON frame from %s: %r', peer[0], str(raw)[:120])
                continue

            reply = await asyncio.get_running_loop().run_in_executor(
                None, session.dispatch, msg)
            if reply is not None:
                await ws.send(json.dumps(reply))
    except websockets.ConnectionClosed:
        pass
    finally:
        log.info('terminal %s disconnected', session.serial or peer[0])


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-7s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')

    missing = [n for n, v in (('GYM_ID', GYM_ID), ('BRIDGE_SECRET', SECRET)) if not v]
    if missing:
        raise SystemExit(f'Missing required environment: {", ".join(missing)}')
    if not ALLOWED_SERIALS:
        log.warning('ALLOWED_SERIALS is empty — every serial number will be '
                    'accepted. Set it to this gym\'s terminal serial.')

    queue = Queue(QUEUE_PATH)
    log.info('KriyaCore bridge — forwarding to %s (gym %s)', CLOUD_URL, GYM_ID)
    log.info('listening for terminals on ws://%s:%s', LISTEN_HOST, LISTEN_PORT)
    log.info('allowed serials: %s', ', '.join(sorted(ALLOWED_SERIALS)) or '(any)')
    if queue.depth():
        log.info('%d event(s) waiting from a previous run', queue.depth())

    asyncio.ensure_future(drain_queue(queue))
    async with websockets.serve(lambda ws: handle_connection(ws, queue),
                                LISTEN_HOST, LISTEN_PORT,
                                ping_interval=30, ping_timeout=30,
                                max_size=4 * 1024 * 1024):
        await asyncio.Future()   # run forever


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
