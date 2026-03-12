#!/usr/bin/env python3
"""
Monome Grid Bridge
===================

High-level overview
-------------------
This script runs on a Raspberry Pi and acts as the *host side* of the system.
It connects a Monome Grid 128 to an Expert Sleepers Disting NT running a Lua
algorithm. The Pi:

- talks to the Grid over OSC via `serialosc` (using `pymonome`),
- talks to the Disting NT over USB MIDI (using `mido`/`python-rtmidi`),
- keeps an in-memory representation of the 16×8 grid state, and
- renders that state to the physical Grid efficiently.

Data flow
---------
- Grid → NT:
    - When the user presses a key on the Grid, the bridge converts its
      coordinates `(x, y)` to a linear index `i = y * 16 + x`, and sends
      a MIDI Note On/Off to the NT (note = `i`, velocity 127/0).

- NT → Grid:
    - The Lua script on the NT sends semantic MIDI back to the Pi:
        - CC12: value 0 means "clear playhead overlay",
        - CC13: total splice count (0–96); number of dim LEDs on the grid,
        - Note On, vel=100: playhead at index (page-local).
    - The bridge consumes these and updates a 16×8 framebuffer from
      splice_count and playhead only. A full-frame update is pushed to
      the Grid whenever that state changes.

Runtime assumptions
-------------------
- `serialosc` is running and has discovered a Grid device.
- The Grid is 16×8 (Monome 128). For other sizes, `GRID_W/GRID_H` and
  the NT Lua script need to be changed together.
- The NT Lua script uses the semantic protocol described above and its
  `MIDI ch` parameter matches `NT_MIDI_CH_1TO16` below.
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

# MIDI channel for NT (1–16). Must match the Lua script "MIDI ch" parameter on the Disting NT.
NT_MIDI_CH_1TO16 = 1

# Semantic protocol: incoming MIDI from NT → this script.
# In the deterministic model we only care about splice count and playhead.
CC_PLAYHEAD_CLEAR = 12   # CC #12 value 0 = clear playhead (when NT sequencer is stopped)
CC_SPLICE_COUNT = 13     # CC #13 value = total splice count (0..96); drives how many grid LEDs are dim
VEL_PLAYHEAD = 100       # Note On velocity 100 = playhead at index (note = index)
# Note On with other velocities (including 0) are ignored by the bridge.

# Grid LED brightness levels. Serialosc variable brightness is 0–15; devices from
# June 2012+ support all 16 levels; older grids may quantize to 4 levels (see serialosc OSC docs).
BR_OFF = 0
BR_DIM = 4          # Within splice count (available splice); number of dim LEDs = splice count
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
    """Playhead overlay state plus flash timing for blinking the current splice."""
    playhead_idx: Optional[int] = None
    playhead_flash: bool = False
    last_flash_t: float = 0.0


class GridState:
    """
    Mutable framebuffer state for the Grid.

    Tracks:
      - splice_count: total number of *valid* splices (0..96) reported by the NT;
        drives how many grid LEDs are shown as dim (splice count → dim LED state).
      - page: current page (1-based) from NT; together with splice_count it defines
        which grid indices are in-range when more than 128 splices are supported.
      - playhead: which index (0..127) on the current page the NT is currently
        playing. That LED is rendered bright + blinking on top of the dim base.

    Grid positions map to splice indices: for page P, grid index i (0..127)
    corresponds to absolute splice index (P-1)*128 + i + 1 (1-based). Only
    indices 1..splice_count are shown as dim; the rest are off. Playhead
    overlay is drawn on top (bright/blinking) but never beyond splice_count.
    """
    def __init__(self):
        self.overlays = Overlays()
        # Splice count and page from NT (CC13, CC11). Drive how many LEDs are dim.
        # Start from a safe empty state: no dim LEDs until NT sends authoritative state.
        self.splice_count = 0
        self.page = 1
        self.dirty = True

    def set_splice_count(self, n: int):
        # Allow 0 so that "no valid splices" can be represented explicitly.
        n = clamp(n, 0, 127)
        if self.splice_count != n:
            self.splice_count = n
            # If the playhead has moved out of range after a splice-count change,
            # clear it so we do not blink on an invalid splice.
            if self.overlays.playhead_idx is not None:
                page_base = (self.page - 1) * 128
                abs_1based = page_base + self.overlays.playhead_idx + 1
                if not (1 <= abs_1based <= self.splice_count):
                    self.overlays.playhead_idx = None
            self.dirty = True

    def set_page(self, p: int):
        p = clamp(p, 1, 127)
        if self.page != p:
            self.page = p
            self.dirty = True

    def set_playhead(self, idx: Optional[int]):
        if idx is not None:
            idx = clamp(idx, 0, 127)
        if self.overlays.playhead_idx != idx:
            self.overlays.playhead_idx = idx
            self.dirty = True

    def compose_levels(self) -> List[List[int]]:
        """
        Build a 16×8 brightness frame (0–15) from splice count, page, and overlays.

        Mapping rules (must match NT Lua script and user requirements):
          - Splice exists / within current splice count → LED dim (BR_DIM).
          - Currently playing splice → LED bright or blinking (BR_PLAYHEAD).
          - Splice beyond splice count or off-page → LED off (BR_OFF).

        Grid index to absolute splice (1-based): abs_idx = (page - 1) * 128 + idx + 1.
        We iterate over every grid cell (x, y) → linear idx = y*16 + x; if
        1 <= abs_idx <= splice_count, cell is in range and gets BR_DIM. Then
        playhead overlay is applied (bright/blink) only if that index is in range.
        """
        frame = [[BR_OFF for _ in range(GRID_W)] for _ in range(GRID_H)]
        page_base = (self.page - 1) * 128

        for y in range(GRID_H):
            for x in range(GRID_W):
                idx = xy_to_idx(x, y)
                abs_1based = page_base + idx + 1
                if 1 <= abs_1based <= self.splice_count:
                    frame[y][x] = BR_DIM

        if self.overlays.playhead_idx is not None:
            x, y = idx_to_xy(self.overlays.playhead_idx)
            # Only render playhead if it maps to a valid splice index.
            abs_1based = page_base + self.overlays.playhead_idx + 1
            if 1 <= abs_1based <= self.splice_count:
                if self.overlays.playhead_flash:
                    frame[y][x] = max(frame[y][x], BR_PLAYHEAD)
                else:
                    frame[y][x] = max(frame[y][x], BR_DIM)

        return frame


# -----------------------------------------------------------------------------
# MIDI interface (NT in/out)
# -----------------------------------------------------------------------------

class MidiToNt:
    """
    Bi-directional MIDI interface to the Disting NT.

    Responsibilities
    ----------------
    - Discover the correct USB MIDI input/output ports for the NT by
      matching substrings in `mido.get_*_names()`.
    - Open those ports and expose a simple API for:
        * sending Note On/Off and Control Change messages on the NT's
          configured channel,
        * receiving any MIDI from the NT and pushing it into an
          `asyncio.Queue` for the main event loop to consume.

    Threading model
    ---------------
    - `mido` invokes the input callback on a background thread whenever
      a MIDI message arrives.
    - We bridge that to asyncio with `loop.call_soon_threadsafe`,
      which schedules `rx_queue.put_nowait(msg)` on the main event loop.
    - The rest of the code (`midi_rx_loop`) treats `rx_queue` as a
      normal async source of messages and does not need to care that
      the data came from another thread.
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
    Monome Grid client application.

    This class is responsible for:
      - receiving key events from `serialosc`/`pymonome`,
      - converting grid coordinates to a linear index understood by the NT,
      - sending the appropriate Note On/Off to the NT via `MidiToNt`,
      - owning a `GridBuffer` used by the render loop to push LED frames.

    Important: key handlers *do not* drive LEDs directly. Instead, LEDs are
    a pure function of semantic MIDI state from the NT (encoded in a
    `GridState` instance) and are rendered by `render_loop` at up to 60 FPS.
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
    Periodically render the logical grid state to the physical Grid.

    This coroutine:
      - runs at up to 60 FPS,
      - toggles a `playhead_flash` flag at ~4 Hz to make the playhead blink,
      - asks `GridState` to compose a brightness matrix whenever the state
        has changed (dirty flag),
      - writes that matrix into `app.buffer` and flushes it to the Grid.

    It never blocks on I/O other than the sleep, so MIDI reception and
    other asyncio tasks (e.g. `midi_rx_loop`) keep running smoothly.
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
    """
    Consume semantic MIDI messages from the NT and update `GridState`.

    Interpretation rules (must match the NT Lua script):
      - CC12: value 0 clears the playhead overlay entirely,
      - CC13: value 0..96 sets the splice count (number of dim LEDs),
      - Note On vel = 100: mark the given index as the playhead.

    Any time `GridState` changes, its `dirty` flag is set and the
    render loop will send a new frame to the Grid.
    """
    while True:
        msg = await midi.rx_queue.get()

        # Protocol: CC12 value 0 = clear playhead; CC13 = splice count
        if msg.type == "control_change":
            if msg.control == CC_PLAYHEAD_CLEAR and msg.value == 0:
                state.set_playhead(None)
            elif msg.control == CC_SPLICE_COUNT:
                state.set_splice_count(msg.value)

        elif msg.type == "note_on":
            idx = msg.note
            vel = msg.velocity

            if vel == VEL_PLAYHEAD:
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

    # Request an authoritative state dump on startup so the grid does not show
    # stale or default splice counts. The Lua script should respond by sending
    # CC13 (splice_count) and playhead/clear as appropriate when it sees CC127.
    try:
        midi.send_cc(127, 1)
    except Exception:
        pass

    tasks = [
        asyncio.create_task(midi_rx_loop(midi, state)),
        asyncio.create_task(render_loop(app, state)),
    ]

    print("Running. Grid key → NT notes. NT semantic MIDI → grid LEDs.")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())