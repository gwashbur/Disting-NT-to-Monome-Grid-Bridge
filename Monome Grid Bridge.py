#!/usr/bin/env python3
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, List

import monome  # provided by pip package "pymonome"
import mido


# -----------------------------
# Configuration
# -----------------------------

GRID_W = 16
GRID_H = 8

# MIDI port name matching (tweak if your NT enumerates differently)
NT_PORT_MATCH = ["disting", "expert", "sleepers", "nt"]

# Outgoing MIDI channel to NT (1..16 in UI, 0..15 in MIDI bytes)
NT_MIDI_CH_1TO16 = 1

# Semantic protocol (incoming from NT -> Pi)
CC_CURSOR = 10       # CC10 value = index 0..127 cursor
VEL_ENABLED = 20     # NoteOn vel=20 means enabled
VEL_PLAYHEAD = 100   # NoteOn vel=100 means playhead position
# NoteOn vel=0 means disabled (treated like off)


# Brightness semantics (0..15) on grid
BR_OFF = 0
BR_EXIST = 3         # optional (unused in this POC unless you want)
BR_ENABLED = 6
BR_CURSOR = 12
BR_PLAYHEAD = 15


def clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def idx_to_xy(idx: int):
    idx = clamp(idx, 0, 127)
    return idx % GRID_W, idx // GRID_W


def xy_to_idx(x: int, y: int) -> int:
    return (y * GRID_W) + x


# -----------------------------
# Framebuffer + Overlays
# -----------------------------

@dataclass
class Overlays:
    cursor_idx: Optional[int] = None
    playhead_idx: Optional[int] = None
    playhead_flash: bool = False
    last_flash_t: float = 0.0


class GridState:
    """
    Base cell states driven by semantic MIDI from NT.
    We keep it simple: enabled bitmap + overlays.
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
        """
        Compose a 16x8 brightness frame (0..15) from base + overlays.
        """
        frame = [[BR_OFF for _ in range(GRID_W)] for _ in range(GRID_H)]

        # Base enabled layer
        for idx, on in enumerate(self.enabled):
            if on:
                x, y = idx_to_xy(idx)
                frame[y][x] = BR_ENABLED

        # Overlays: cursor and playhead
        if self.overlays.cursor_idx is not None:
            x, y = idx_to_xy(self.overlays.cursor_idx)
            frame[y][x] = max(frame[y][x], BR_CURSOR)

        if self.overlays.playhead_idx is not None:
            x, y = idx_to_xy(self.overlays.playhead_idx)
            # Optional flash: alternate between full and enabled-ish
            if self.overlays.playhead_flash:
                frame[y][x] = max(frame[y][x], BR_PLAYHEAD)
            else:
                frame[y][x] = max(frame[y][x], BR_ENABLED)

        return frame


# -----------------------------
# MIDI Interface
# -----------------------------

class MidiToNt:
    """
    Handles auto-detect, input callback -> asyncio queue,
    and output sending.
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
        """
        Loop until both input and output ports are found and opened.
        """
        while True:
            ins = mido.get_input_names()
            outs = mido.get_output_names()

            in_name = next((n for n in ins if self._match_port(n)), None)
            out_name = next((n for n in outs if self._match_port(n)), None)

            if not in_name or not out_name:
                print("[MIDI] Waiting for Disting NT ports...")
                await asyncio.sleep(0.5)
                continue

            # Open ports (close old if reconnected)
            if in_name != self._connected_name_in:
                if self.inport:
                    try: self.inport.close()
                    except: pass
                self.inport = mido.open_input(in_name, callback=self._mido_callback)
                self._connected_name_in = in_name

            if out_name != self._connected_name_out:
                if self.outport:
                    try: self.outport.close()
                    except: pass
                self.outport = mido.open_output(out_name)
                self._connected_name_out = out_name

            print(f"[MIDI] Connected IN : {self._connected_name_in}")
            print(f"[MIDI] Connected OUT: {self._connected_name_out}")
            return

    def _mido_callback(self, msg):
        # mido invokes callbacks from a background thread; hop into asyncio loop safely
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
        # NoteOn velocity 0 is a valid "off" in MIDI practice; we keep explicit on/off
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


# -----------------------------
# Monome Grid App
# -----------------------------

class GridBridge(monome.GridApp):
    """
    Receives grid key events and sends MIDI to NT.
    Rendering is handled by a separate render loop using GridBuffer.
    """
    def __init__(self, midi: MidiToNt):
        super().__init__()
        self.midi = midi
        self.connected = False
        self.buffer = monome.GridBuffer(GRID_W, GRID_H)

    async def connect_grid(self, host: str, port: int):
        await self.grid.connect(host, port)
        self.connected = True
        print("[GRID] Connected to device port:", port)

    def on_grid_key(self, x: int, y: int, s: int):
        # Map directly 1:1
        if not (0 <= x < GRID_W and 0 <= y < GRID_H):
            return
        idx = xy_to_idx(x, y)

        if s == 1:
            # Press -> Note On
            self.midi.send_note_on(idx, 127)
        else:
            # Release -> Note Off
            self.midi.send_note_off(idx)


# -----------------------------
# Main application loops
# -----------------------------

async def render_loop(app: GridBridge, state: GridState):
    """
    Fast full-frame updates via GridBuffer.
    Only flush when dirty or when playhead flash toggles.
    """
    FPS_LIMIT = 60.0
    min_dt = 1.0 / FPS_LIMIT

    while True:
        start = time.time()

        # Flash playhead at ~4Hz (optional)
        now = start
        if state.overlays.playhead_idx is not None:
            if (now - state.overlays.last_flash_t) > 0.125:  # ~8 toggles/sec (4Hz blink)
                state.overlays.last_flash_t = now
                state.overlays.playhead_flash = not state.overlays.playhead_flash
                state.dirty = True

        if app.connected and state.dirty:
            frame = state.compose_levels()

            # Update buffer in-memory, then render once
            # GridBuffer expects levels 0..15; we set per cell
            for y in range(GRID_H):
                for x in range(GRID_W):
                    app.buffer.led_level_set(x, y, frame[y][x])

            app.buffer.render(app.grid)
            state.dirty = False

        elapsed = time.time() - start
        await asyncio.sleep(max(0.0, min_dt - elapsed))


async def midi_rx_loop(midi: MidiToNt, state: GridState):
    """
    Consume incoming MIDI from NT and update semantic state.
    """
    while True:
        msg = await midi.rx_queue.get()

        # We only handle what we defined in the semantic protocol:
        # - CC10: cursor index
        # - NoteOn vel=20: enabled (note=index)
        # - NoteOn vel=0: disabled
        # - NoteOn vel=100: playhead (note=index)
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
            else:
                # Optional: treat any other nonzero vel as "enabled"
                # Comment this out if you want strict protocol enforcement.
                pass

        elif msg.type == "note_off":
            # Optional: ignore
            pass


async def main():
    loop = asyncio.get_running_loop()

    # 1) MIDI connect to NT
    midi = MidiToNt(loop)
    await midi.connect()

    # 2) Grid serialosc discovery + connect
    app = GridBridge(midi)
    serialosc = monome.SerialOsc()

    def on_device_added(dev_id, dev_type, dev_port):
        # connect to the first discovered grid
        print(f"[GRID] Discovered id={dev_id} type={dev_type} port={dev_port}")
        asyncio.ensure_future(app.connect_grid("127.0.0.1", dev_port))

    serialosc.device_added_event.add_handler(on_device_added)

    print("[GRID] Connecting to serialosc...")
    await serialosc.connect()

    # 3) State + loops
    state = GridState()

    # Optional: ask NT for a full redraw on startup (if your NT script supports CC127 request)
    # midi.send_cc(127, 1)

    tasks = [
        asyncio.create_task(midi_rx_loop(midi, state)),
        asyncio.create_task(render_loop(app, state)),
    ]

    print("Running. Grid press -> NT notes. NT semantic MIDI -> grid LEDs.")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())