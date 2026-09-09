# KriyaCore Face ID bridge

Runs **at the gym**, on the same network as the face terminal. Not on the server.

## Why it exists

The Mivanta terminal (protocol 2.1) has two properties that make it unsafe to
point at the internet:

- **No TLS.** The spec says so outright — *"the default listen port is 7788,
  no TLS encrypt"* — and the device speaks `ws://` only, so no proxy on our
  side can add it.
- **No real authentication.** A terminal identifies itself with its serial
  number and nothing else. Anyone who can reach port 7788 can register as a
  terminal and push fabricated attendance.

So the plaintext half stays inside the building, and this bridge is the only
thing that talks to KriyaCore — over HTTPS, signed, with replay protection.

```
GYM LAN                                  INTERNET          BANGALORE VPS
┌──────────────────────┐                                  ┌────────────┐
│ terminal ─ws://:7788─┼─► bridge ── HTTPS + HMAC ─────────┼─► KriyaCore│
└──────────────────────┘                                  └────────────┘
   plaintext, never leaves the building
```

## What it guarantees

| | How |
|---|---|
| Port 7788 unreachable from the internet | It only ever listens on the gym LAN; the VPS firewall stays SSH/80/443 |
| No spoofed terminal | Serial-number allowlist — an unknown `sn` is refused at registration |
| No forged scans reaching the cloud | HMAC-SHA256 over `timestamp.body`, rejected outside a 2-minute window |
| No replayed scans | The protocol's own `logindex` counter; anything at or below the last accepted index is ignored |
| No biometric data leaves the LAN | The device's base64 `image` field is stripped before anything is sent or queued |
| Door still works when the internet drops | Fails **open** by default and queues the scan to disk; drains automatically |

## Hardware

A Raspberry Pi 4 (₹4–5k) or the gym's existing front-desk PC. It needs to be
on the same network as the terminal and stay powered on.

## Install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Point the terminal at this machine's LAN IP, port 7788, in its network
settings. Then:

```bash
export KRIYACORE_URL=https://kriyacore.in
export GYM_ID=1
export BRIDGE_SECRET=<the gym's face_id_webhook_secret>
export ALLOWED_SERIALS=ZX0006827500     # this gym's terminal serial
./venv/bin/python kriyacore_bridge.py
```

`BRIDGE_SECRET` is the gym's `face_id_webhook_secret`. Treat it like a
password — it is the only real credential in the whole chain.

### Options

| Variable | Default | Meaning |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | Set to the LAN IP to avoid listening on every interface |
| `LISTEN_PORT` | `7788` | The device's default |
| `CLOUD_TIMEOUT` | `3.0` | Seconds to wait before the door decision can't wait longer |
| `FAIL_OPEN` | `true` | Door behaviour when KriyaCore is unreachable |
| `QUEUE_PATH` | `bridge_queue.db` | Where unsent scans are buffered |

`FAIL_OPEN=true` matches the app's own `face_id_deny_expired` default:
wrongly locking a paying member out is worse than letting a lapsed one in,
and staff are told either way once the queue drains.

## Run it as a service

```bash
sudo cp kriyacore-bridge.service /etc/systemd/system/
sudo nano /etc/systemd/system/kriyacore-bridge.service   # fill in the values
sudo systemctl daemon-reload
sudo systemctl enable --now kriyacore-bridge
sudo journalctl -u kriyacore-bridge -f
```

## Testing without hardware

`mock_terminal.py` speaks the same protocol the real device does:

```bash
./venv/bin/python mock_terminal.py --enrollid 1              # a normal scan
./venv/bin/python mock_terminal.py --serial WRONG            # must be refused
./venv/bin/python mock_terminal.py --replay                  # must be ignored
./venv/bin/python mock_terminal.py --with-image              # image must be stripped
./venv/bin/python mock_terminal.py --door-event              # enrollid 0, not a person
```

## Enrolling a member

1. Enrol their face **on the terminal**, which assigns an `enrollid`.
2. In KriyaCore, open the member and enter that number under Face ID Access,
   with their consent ticked.

KriyaCore stores only that number. The face template stays on the device —
which is what keeps you a holder of a pseudonymous ID rather than of
biometric data.

## Not implemented

The protocol also lets the server push commands to the terminal — remote door
open, user sync, clearing logs. The bridge only handles the terminal-initiated
half (`reg`, `sendlog`, `senduser`), because that is all the access-control
feature needs. The rest is a straightforward addition to `Session.dispatch`
if it's ever wanted.
