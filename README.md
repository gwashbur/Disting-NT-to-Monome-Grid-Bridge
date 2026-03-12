# Disting NT ↔ Monome Grid Bridge (Raspberry Pi 5)

Connects a **Disting NT** (Lua algorithm) to a **Monome Grid 128** via a **Raspberry Pi 5**: Grid key events → NT; NT state → grid LEDs.

**Stack:** `pymonome`, `mido`, `python-rtmidi`, `asyncio`. Grid over OSC (serialosc); NT over USB MIDI.

## Parts

- **Lua** (`Morphagene L-System Splice Stepper 30.lua`): Deterministic splice stepper for Morphagene. OUT1 = ORGANIZE CV (0–5 V), OUT2 = PLAY gate. Sends splice count and playhead to the bridge.
- **Python** (`Monome Grid Bridge.py`): Discovers grid (serialosc) and NT (MIDI). Keeps `splice_count` and `playhead`; renders dim LEDs for valid splices, bright blinking for current splice. Sends CC127 on startup to request state.
- **systemd:** `serialoscd.service`, `monome-grid-bridge.service` for autostart.

## Protocol

**Grid → NT:** Note On/Off, `note = y*16 + x`, vel 127/0.

**NT → Grid (bridge uses only these):**

| Message | Meaning |
|---------|---------|
| CC12 value 0 | Clear playhead |
| CC13 value 0–96 | Splice count (how many dim LEDs) |
| Note On vel 100 | Playhead at index (page-local) |

**Startup:** Bridge sends CC127; Lua replies with CC13 and playhead or CC12 clear.

## LED behavior

- **Dim** = valid splice (index 1..splice_count), not playing.
- **Bright + blinking** = current playing splice.
- **Off** = out of range or splice_count 0.

Bridge starts with `splice_count = 0` (no dim LEDs) until Lua responds.

## Requirements

Raspberry Pi 5, Disting NT, Monome Grid 128, `serialosc` (build from source on Raspberry Pi OS).

## Install

1. System deps: `sudo apt install -y python3-pip git liblo-dev libudev-dev libavahi-compat-libdnssd-dev libuv1-dev`
2. Build serialosc: clone [libmonome](https://github.com/monome/libmonome), then [serialosc](https://github.com/monome/serialosc) (./waf configure && ./waf && sudo ./waf install). `sudo ldconfig`.
3. Optional USB: `sudo gpasswd -a $USER uucp dialout` then re-login.
4. Python: `cd /path/to/repo && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

## Run

```bash
serialoscd
python3 "Monome Grid Bridge.py"
```

## Start at boot

```bash
sudo cp systemd/serialoscd.service systemd/monome-grid-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable serialoscd.service monome-grid-bridge.service
sudo systemctl start serialoscd.service monome-grid-bridge.service
```

If not user `admin`, edit `.service` files for `User=` and paths. Port 12002 must be free for serialosc.

## Mapping

Grid index `i = y*16 + x` (0–127). Page P maps to absolute splice `(P-1)*128 + i + 1`. Only indices 1..splice_count are dim; playhead overlay only if in range.
