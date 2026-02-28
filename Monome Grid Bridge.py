#!/usr/bin/env python3
"""
Monome Grid Bridge: bridges a Monome grid to Expert Sleepers Disting NT via MIDI.

- Grid key presses are sent to the NT as MIDI notes (index = note number).
- Incoming MIDI from the NT (cursor CC, enabled/playhead note velocities) drives
  grid LED state. Uses pymonome for grid I/O and mido for MIDI.

Requires serialosc to be running (see https://monome.org/docs/serialosc). Grid size
is assumed 16×8 (128 keys); other sizes would need different GRID_W/GRID_H.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, List

import monome  # pymonome
import mido


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Grid dimensions (must match your device; serialosc reports /sys/size as rows cols, e.g. 8 16).
GRID_W = 16
GRID_H = 8

# MIDI port name substrings used to find the Disting NT (adjust if your device name differs).
NT_PORT_MATCH = ["disting", "expert", "sleepers", "nt"]

# MIDI channel used for NT (1–16 in UI; stored and sent as 0–15 in bytes).
NT_MIDI_CH_1TO16 = 1

# Semantic protocol: incoming MIDI from NT → this script.
CC_CURSOR = 10       # CC #10 value = cursor index 0..127
VEL_ENABLED = 20     # Note On velocity 20 = step enabled (note = index)
VEL_PLAYHEAD = 100   # Note On velocity 100 = playhead at index (note = index)
# Note On velocity 0 = step disabled (or use Note Off).

# Grid LED brightness levels. Serialosc variable brightness is 0–15; devices from
# June 2012+ support all 16 levels; older grids may quantize to 4 levels (see serialosc OSC docs).
BR_OFF = 0
BR_EXIST = 3         # Reserved; unused in current logic
BR_ENABLED = 6
BR_CURSOR = 12
BR_PLAYHEAD = 15


def clamp(v: int, lo: int, hi: int) -> int:
    """Clamp integer v to [lo, hi]."""
    return lo if v < lo else hi if v > hi else v


def idx_to_xy(idx: int):
    """Convert linear index 0..127 to grid (x, y)."""
    idx = clamp(idx, 0, 127)
    return idx % GRID_W, idx // GRID_W


def xy_to_idx(x: int, y: int) -> int:
    """Convert grid (x, y) to linear index."""
    return (y * GRID_W) + x


# -----------------------------------------------------------------------------
# Framebuffer and overlays
# -----------------------------------------------------------------------------

@dataclass
class Overlays:
    """Cursor and playhead overlay state plus flash timing."""
    cursor_idx: Optional[int] = None
    playhead_idx: Optional[int] = None
    playhead_flash: bool = False
    last_flash_t: float = 0.0


class GridState:
    """
    Cell state driven by NT semantic MIDI: which steps are enabled,
    plus cursor and playhead overlays. Used to compose the grid frame.
    """
    def __init__(self):
        self.enabled = [False] * 128
        self.overlays = Overlays()
        self.dirty = True

    def set_enabled(self, idx: int, is_on: bool):
        idx = clamp(idx, 0, 127)
        if self.enabled[idx] != is_on:
            self.enabled[idx] = is_on
            self.dirty = True

    def set_cursor(self, idx: Optional[int]):
        if idx is not None:
            idx = clamp(idx, 0, 127)
        if self.overlays.cursor_idx != idx:
            self.overlays.cursor_idx = idx
            self.dirty = True

    def set_playhead(self, idx: Optional[int]):
        if idx is not None:
            idx = clamp(idx, 0, 127)
        if self.overlays.playhead_idx != idx:
            self.overlays.playhead_idx = idx
            self.dirty = True

    def compose_levels(self) -> List[List[int]]:
        """Build a 16×8 brightness frame (0–15) from enabled steps and overlays."""
        frame = [[BR_OFF for _ in range(GRID_W)] for _ in range(GRID_H)]

        for idx, on in enumerate(self.enabled):
            if on:
                x, y = idx_to_xy(idx)
                frame[y][x] = BR_ENABLED

        if self.overlays.cursor_idx is not None:
            x, y = idx_to_xy(self.overlays.cursor_idx)
            frame[y][x] = max(frame[y][x], BR_CURSOR)

        if self.overlays.playhead_idx is not None:
            x, y = idx_to_xy(self.overlays.playhead_idx)
            if self.overlays.playhead_flash:
                frame[y][x] = max(frame[y][x], BR_PLAYHEAD)
            else:
                frame[y][x] = max(frame[y][x], BR_ENABLED)

        return frame


# -----------------------------------------------------------------------------
# MIDI interface (NT in/out)
# -----------------------------------------------------------------------------

class MidiToNt:
    """
    NT MIDI: port auto-detect, incoming messages via asyncio queue,
    and outgoing note/CC send. Callbacks from mido run on a background thread
    and are forwarded into the event loop.
    """
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.inport = None
        self.outport = None
        self.rx_queue: asyncio.Queue = asyncio.Queue()
        self._connected_name_in = None
        self._connected_name_out = None

    @staticmethod
    def _match_port(name: str) -> bool:
        lname = name.lower()
        return any(tok in lname for tok in NT_PORT_MATCH)

    async def connect(self):
        """Block until both NT MIDI input and output ports are available and open."""
        while True:
            ins = mido.get_input_names()
            outs = mido.get_output_names()

            in_name = next((n for n in ins if self._match_port(n)), None)
            out_name = next((n for n in outs if self._match_port(n)), None)

            if not in_name or not out_name:
                print("[MIDI] Waiting for Disting NT ports...")
                await asyncio.sleep(0.5)
                continue

            if in_name != self._connected_name_in:
                if self.inport:
                    try:
                        self.inport.close()
                    except Exception:
                        pass
                self.inport = mido.open_input(in_name, callback=self._mido_callback)
                self._connected_name_in = in_name

            if out_name != self._connected_name_out:
                if self.outport:
                    try:
                        self.outport.close()
                    except Exception:
                        pass
                self.outport = mido.open_output(out_name)
                self._connected_name_out = out_name

            print(f"[MIDI] Connected IN : {self._connected_name_in}")
            print(f"[MIDI] Connected OUT: {self._connected_name_out}")
            return

    def _mido_callback(self, msg):
        """Run on mido's thread; enqueue message for the asyncio loop."""
        self.loop.call_soon_threadsafe(self.rx_queue.put_nowait, msg)

    def send_note(self, note: int, vel: int, on: bool = True):
        if not self.outport:
            return
        note = clamp(note, 0, 127)
        vel = clamp(vel, 0, 127)
        ch = clamp(NT_MIDI_CH_1TO16, 1, 16) - 1
        msg_type = "note_on" if on else "note_off"
        msg = mido.Message(msg_type, channel=ch, note=note, velocity=vel)
        self.outport.send(msg)

    def send_note_on(self, note: int, vel: int):
        """Send Note On (explicit on; we do not use velocity 0 as off)."""
        self.send_note(note, vel, on=True)

    def send_note_off(self, note: int):
        self.send_note(note, 0, on=False)

    def send_cc(self, cc: int, val: int):
        if not self.outport:
            return
        cc = clamp(cc, 0, 127)
        val = clamp(val, 0, 127)
        ch = clamp(NT_MIDI_CH_1TO16, 1, 16) - 1
        msg = mido.Message("control_change", channel=ch, control=cc, value=val)
        self.outport.send(msg)


# -----------------------------------------------------------------------------
# Monome grid app
# -----------------------------------------------------------------------------

class GridBridge(monome.GridApp):
    """
    Monome grid app: key events are sent to the NT as MIDI notes.
    LED state is driven by a separate render loop using GridState (not key handlers).
    Key events follow serialosc /grid/key: (x, y, s) with s=1 down, s=0 up.
    """
    def __init__(self, midi: MidiToNt):
        super().__init__()
        self.midi = midi
        self.connected = False
        self.buffer = monome.GridBuffer(GRID_W, GRID_H)

    async def connect_grid(self, host: str, port: int):
        """Connect to the grid via serialosc device port (serialosc spawns one port per device)."""
        await self.grid.connect(host, port)
        self.connected = True
        print("[GRID] Connected to device port:", port)

    def on_grid_key(self, x: int, y: int, s: int):
        """Map grid key (x, y) to linear index; send Note On/Off to NT."""
        if not (0 <= x < GRID_W and 0 <= y < GRID_H):
            return
        idx = xy_to_idx(x, y)

        if s == 1:
            self.midi.send_note_on(idx, 127)
        else:
            self.midi.send_note_off(idx)


# -----------------------------------------------------------------------------
# Main loops: render and MIDI RX
# -----------------------------------------------------------------------------

async def render_loop(app: GridBridge, state: GridState):
    """
    Periodically compose the grid frame from state and push to the device.
    Runs at up to 60 FPS; only flushes when state is dirty or playhead flash toggles.
    """
    FPS_LIMIT = 60.0
    min_dt = 1.0 / FPS_LIMIT

    while True:
        start = time.time()

        # Toggle playhead brightness at ~4 Hz when playhead is set
        now = start
        if state.overlays.playhead_idx is not None:
            if (now - state.overlays.last_flash_t) > 0.125:
                state.overlays.last_flash_t = now
                state.overlays.playhead_flash = not state.overlays.playhead_flash
                state.dirty = True

        if app.connected and state.dirty:
            frame = state.compose_levels()
            # Serialosc: /grid/led/level/set x y l with l in [0, 15]; GridBuffer sends level per cell.
            for y in range(GRID_H):
                for x in range(GRID_W):
                    app.buffer.led_level_set(x, y, frame[y][x])

            app.buffer.render(app.grid)
            state.dirty = False

        elapsed = time.time() - start
        await asyncio.sleep(max(0.0, min_dt - elapsed))


async def midi_rx_loop(midi: MidiToNt, state: GridState):
    """Process NT MIDI from the queue and update GridState (cursor, enabled, playhead)."""
    while True:
        msg = await midi.rx_queue.get()

        # Protocol: CC10 = cursor index; Note On vel 20 = enabled, vel 100 = playhead, vel 0 = disabled
        if msg.type == "control_change":
            if msg.control == CC_CURSOR:
                state.set_cursor(msg.value)

        elif msg.type == "note_on":
            idx = msg.note
            vel = msg.velocity

            if vel == 0:
                state.set_enabled(idx, False)
            elif vel == VEL_ENABLED:
                state.set_enabled(idx, True)
            elif vel == VEL_PLAYHEAD:
                state.set_playhead(idx)
            # Other nonzero velocities are ignored (strict protocol)

        elif msg.type == "note_off":
            pass


async def main():
    loop = asyncio.get_running_loop()

    midi = MidiToNt(loop)
    await midi.connect()

    app = GridBridge(midi)
    serialosc = monome.SerialOsc()  # Discovery: serialosc server port 12002; /serialosc/list, /serialosc/notify

    def on_device_added(dev_id, dev_type, dev_port):
        print(f"[GRID] Discovered id={dev_id} type={dev_type} port={dev_port}")
        asyncio.ensure_future(app.connect_grid("127.0.0.1", dev_port))

    serialosc.device_added_event.add_handler(on_device_added)

    print("[GRID] Connecting to serialosc...")
    await serialosc.connect()

    state = GridState()

    # Optional: request full redraw from NT on startup if it supports e.g. CC 127
    # midi.send_cc(127, 1)

    tasks = [
        asyncio.create_task(midi_rx_loop(midi, state)),
        asyncio.create_task(render_loop(app, state)),
    ]

    print("Running. Grid key → NT notes. NT semantic MIDI → grid LEDs.")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())