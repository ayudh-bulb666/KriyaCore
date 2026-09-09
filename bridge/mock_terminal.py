#!/usr/bin/env python3
"""A fake Mivanta face terminal, for testing the bridge without hardware.

Speaks the same WebSocket + JSON protocol 2.1 the real device does: connects
out, registers with a serial number, then pushes scan events and prints what
the bridge answers — including the `access` flag that opens or refuses the
door.

Useful before the hardware arrives, and afterwards for reproducing problems
without standing at a door.

    python3 mock_terminal.py --serial ZX0006827500 --enrollid 1
    python3 mock_terminal.py --serial WRONG-SERIAL      # must be refused
    python3 mock_terminal.py --replay                   # must be ignored
    python3 mock_terminal.py --with-image               # image must be stripped
"""
import argparse
import asyncio
import base64
import json
from datetime import datetime

import websockets


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


REG = {
    'cmd': 'reg',
    'sn': 'ZX0006827500',
    'cpusn': '123456789',
    'devinfo': {
        'modelname': 'tfs30', 'usersize': 3000, 'fpsize': 3000,
        'cardsize': 3000, 'pwdsize': 3000, 'logsize': 100000,
        'useduser': 12, 'usedfp': 0, 'usedcard': 0, 'usedpwd': 0,
        'usedlog': 40, 'usednewlog': 3, 'fpalgo': 'thbio3.0',
        'firmware': 'th600w v6.1', 'time': now(),
        'mac': '00-01-A9-01-00-01',
    },
}


def scan(enrollid, logindex, with_image=False):
    """One face scan, shaped exactly like the protocol doc's example."""
    rec = {
        'enrollid': enrollid,
        'time': now(),
        'mode': 8,          # 8 == face
        'inout': 0,         # 0 == in
        'event': 0,
        'temp': 36.5,
        'verifymode': 13,
    }
    if with_image:
        # The real device sends a base64 JPEG of the person's face here.
        # The bridge must drop this before it leaves the LAN.
        rec['image'] = base64.b64encode(b'\xff\xd8\xff' + b'FAKE-FACE-JPEG' * 40).decode()
    return {'cmd': 'sendlog', 'sn': REG['sn'], 'count': 1,
            'logindex': logindex, 'record': [rec]}


async def run(args):
    url = f'ws://{args.host}:{args.port}'
    print(f'connecting to {url} ...')
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        reg = dict(REG, sn=args.serial)
        await ws.send(json.dumps(reg))
        reply = json.loads(await ws.recv())
        print(f'  reg      -> {reply}')
        if not reply.get('result'):
            print('\n  registration refused — the bridge rejected this serial.')
            return

        idx = args.logindex
        await ws.send(json.dumps(scan(args.enrollid, idx, args.with_image)))
        reply = json.loads(await ws.recv())
        door = 'OPEN' if reply.get('access') else 'REFUSED'
        print(f'  sendlog  -> access={reply.get("access")}  [{door}]  {reply}')

        if args.replay:
            print('\n  replaying the same logindex (should be ignored) ...')
            await ws.send(json.dumps(scan(args.enrollid, idx, args.with_image)))
            reply = json.loads(await ws.recv())
            print(f'  replay   -> {reply}')

        if args.door_event:
            print('\n  sending a door event (enrollid 0, not a person) ...')
            await ws.send(json.dumps({
                'cmd': 'sendlog', 'sn': args.serial, 'count': 1,
                'logindex': idx + 1,
                'record': [{'enrollid': 0, 'time': now(), 'mode': 0,
                            'inout': 1, 'event': 1}]}))
            print(f'  door     -> {json.loads(await ws.recv())}')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=7788)
    p.add_argument('--serial', default='ZX0006827500')
    p.add_argument('--enrollid', type=int, default=1)
    p.add_argument('--logindex', type=int, default=100)
    p.add_argument('--with-image', action='store_true',
                   help='attach a base64 face image, as the real device can')
    p.add_argument('--replay', action='store_true',
                   help='send the same logindex twice')
    p.add_argument('--door-event', action='store_true',
                   help='also send an enrollid=0 door event')
    asyncio.run(run(p.parse_args()))
