Disting NT ↔ Monome Grid Bridge (Raspberry Pi 5)
===============================================

Project purpose
---------------
This project connects an Expert Sleepers **Disting NT** running a custom Lua
algorithm to a **Monome Grid 128**, using a **Raspberry Pi 5** as the host.

The system is split into two cooperating parts:

- **Lua algorithm on the Disting NT**  
  (`Morphagene L-System Splice Stepper 30.lua`)  
  Implements a Morphagene-oriented L-system splice stepper, drives ORGANIZE
  and PLAY outputs, and emits *semantic MIDI* describing its UI state
  (cursor, enabled steps, playhead).

- **Python bridge on the Raspberry Pi**  
  (`Monome Grid Bridge.py`)  
  Talks to the Grid over OSC (via `serialosc`/`pymonome`) and to the NT
  over USB MIDI (via `mido`/`python-rtmidi`), maintains a framebuffer for
  the 16×8 grid, and renders the Lua script's state to the Grid.

The result is a tightly synchronized 1:1 mapping between the Grid (16×8)
and NT UI state, with semantic mirroring and flicker-free full-frame updates.

Implementation stack
--------------------
- `pymonome` (serialosc client for Monome devices)
- `mido` + `python-rtmidi` (MIDI I/O)
- `asyncio`-based event architecture on the Pi

Overview
--------
The bridge provides:
1:1 coordinate mapping (Grid 16×8 → NT 128-step model)
Semantic MIDI protocol for state mirroring
Buffered LED rendering via GridBuffer
Overlay composition (cursor + playhead)
Clean separation between logic and rendering
Auto-detection of Disting NT MIDI ports
Serialosc device discovery for monome Grid

Designed for:
Sequencer control
Morphagene splice navigation
L-system step walkers
Generative pattern systems
Custom NT Lua UI integration
Architecture

Signal flow:

Monome Grid
     │
     │ (OSC via serialosc)
     ▼
Raspberry Pi 5 (Python bridge)
     │
     │ (USB MIDI)
     ▼
Disting NT (Lua script)

Layer Breakdown

On Raspberry Pi
MIDI interface (NT USB)
Framebuffer state model (16×8)
Overlay manager
Grid renderer (buffered full-frame updates)

On Disting NT
Lua algorithm
Semantic state emission (Note/CC messages)
Semantic MIDI Protocol
----------------------

Grid → NT (from Pi / Python bridge)

- **Note On**: `note = y*16 + x`, `velocity = 127` (key down)
- **Note Off**: `velocity = 0` (key up)

NT → Grid (from Lua on the Disting NT, consumed by the Pi)

| Message              | Meaning                            |
|----------------------|------------------------------------|
| CC10 value = 0–127   | Cursor index (page-local)         |
| CC11 value = 1–127   | Current page index (1-based)      |
| CC12 value = 0       | Clear playhead overlay            |
| Note On vel = 20     | Enabled step at index (note)      |
| Note On vel = 0      | Disabled step at index (note)     |
| Note On vel = 100    | Playhead at index (note)          |

**Compatible NT algorithm:**  
The Lua script `Morphagene L-System Splice Stepper 30.lua` is designed to
work with this bridge. Load it on the Disting NT; it uses the semantic
protocol above (CC10 cursor, CC12 clear, vel 20/0/100 for
enabled/disabled/playhead) and treats the grid as a 16×8 page window over
up to 96 Morphagene splices (MAX_SPLICES in the Lua script).

This avoids SysEx and keeps bandwidth low while preserving state meaning.

Features
Fast full-frame LED updates using GridBuffer
Dirty-flag rendering (no LED spam)
Playhead flashing overlay
Clean semantic state separation
Designed for extensibility (paging, modes, probability layers)

Scripts and responsibilities
----------------------------

- `Monome Grid Bridge.py`  
  - Discovers the Grid via `serialosc` and connects to it with `pymonome`.  
  - Discovers the Disting NT USB MIDI ports and connects with `mido`.  
  - Converts Grid key events to MIDI notes for the NT.  
  - Receives semantic MIDI from the NT and maintains a 16×8 framebuffer
    (`GridState`) describing enabled steps, cursor, and playhead.  
  - Periodically renders the framebuffer to the Grid using a `GridBuffer`.

- `Morphagene L-System Splice Stepper 30.lua`  
  - Implements a 96-splice L-system step sequencer for Morphagene.  
  - OUT1: Morphagene ORGANIZE CV (0–5 V, bin-centered across splices).  
  - OUT2: 5 V gate to Morphagene PLAY.  
  - Maintains internal state: active splices, enabled list, cursor, playhead,
    and current ORGANIZE voltage.  
  - Emits semantic MIDI (CC/Note messages) to mirror its state to the Grid
    via the Pi bridge.

- `systemd/serialoscd.service`  
  - User service that runs the Monome `serialoscd` daemon under the chosen
    Linux user account and wires it to the journal for logging.

- `systemd/monome-grid-bridge.service`  
  - System service that starts the Python bridge after `serialoscd` is up,
    with a small delay to let USB/OSC settle, and restarts on failure.

Requirements
------------
- Raspberry Pi 5 (Raspberry Pi OS recommended)
- Expert Sleepers Disting NT
- Monome Grid 128
- `serialosc` installed and running

Raspberry Pi documentation
--------------------------
Official setup and reference: [Raspberry Pi Documentation](https://www.raspberrypi.com/documentation/).

- **First-time setup:** Use [Raspberry Pi Imager](https://www.raspberrypi.com/documentation/computers/getting-started.html#install-using-imager) to install Raspberry Pi OS (32 GB+ SD card recommended). You can enable SSH in Imager’s customisation for headless use.
- **Raspberry Pi 5 power:** Use a 5 V/5 A USB‑C supply (e.g. [official 27 W](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#power-supply)); underpowered USB can cause issues with the grid and Disting NT.
- **USB:** The Monome Grid and Disting NT connect over USB; no extra drivers are needed. If serialosc reports “Permission denied”, add your user to `dialout`/`uucp` (see step 3 below).

Installation
------------
**Note:** On Raspberry Pi OS (Debian), `serialosc` is not in the default apt repositories. Install it by building from source as below. On Ubuntu you can use `sudo add-apt-repository ppa:artfwo/monome` then `apt install serialosc` instead.

1. Install system packages and build dependencies
```bash
sudo apt update
sudo apt install -y python3-pip git \
  liblo-dev libudev-dev \
  libavahi-compat-libdnssd-dev libuv1-dev
```

2. Build and install serialosc (required for Monome Grid)
```bash
# libmonome (dependency)
git clone https://github.com/monome/libmonome.git
cd libmonome
./waf configure
./waf
sudo ./waf install
cd ..

# serialosc
git clone https://github.com/monome/serialosc.git --recursive
cd serialosc
./waf configure
./waf
sudo ./waf install
cd ..

sudo ldconfig
```

3. (Optional) USB access for the grid — if you get "Permission denied" when running serialosc:
```bash
sudo gpasswd -a $USER uucp
sudo gpasswd -a $USER dialout
```
Then log out and back in.

4. Install Python dependencies
```bash
cd /path/to/Disting-NT-to-Monome-Grid-Bridge
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

Running
-------
Connect:

- Disting NT via USB
- Monome Grid via USB

Start the serialosc daemon (in a terminal or before the bridge):
```bash
serialoscd
```

Then run the bridge:
```bash
python3 "Monome Grid Bridge.py"
```

Start at boot (systemd)
-----------------------
To have serialosc and the bridge start automatically on boot (recommended on Raspberry Pi):

1. Copy the service files into systemd (run from the project directory):
```bash
cd /home/admin/Disting-NT-to-Monome-Grid-Bridge
sudo cp systemd/serialoscd.service systemd/monome-grid-bridge.service /etc/systemd/system/
```

2. Reload systemd, enable both services, and start them:
```bash
sudo systemctl daemon-reload
sudo systemctl enable serialoscd.service monome-grid-bridge.service
sudo systemctl start serialoscd.service monome-grid-bridge.service
```

3. Check status:
```bash
sudo systemctl status serialoscd.service
sudo systemctl status monome-grid-bridge.service
```

- **serialosc** starts first; the **bridge** starts after a short delay so the grid can be discovered.
- Logs: `journalctl -u serialoscd.service -f` and `journalctl -u monome-grid-bridge.service -f`.
- To stop auto-start: `sudo systemctl disable serialoscd.service monome-grid-bridge.service`.
- **Port 12002:** serialosc uses UDP port 12002. If the service fails with exit code 255, the port is likely in use (e.g. another serialoscd). Run only the systemd service, or only a manual `serialoscd`—not both. To free the port: `sudo systemctl stop serialoscd.service` and `pkill -x serialoscd`, then start the service again.

If your username is not `admin`, edit both `.service` files and change `User=admin` to your user, and update the paths in `monome-grid-bridge.service` to match your home directory before copying to `/etc/systemd/system/`.

You should see:

```text
[MIDI] Connected IN : ...
[MIDI] Connected OUT: ...
[GRID] Connected to device ...
Running. Grid key → NT notes. NT semantic MIDI → grid LEDs.
```

Mapping model
-------------

Index mapping is 1:1 across the system:

- `index = y * 16 + x`  
- Grid `(x, y)`  
- MIDI note number  
- NT internal step index  
- Framebuffer index

Brightness semantics (Grid 0–15)
--------------------------------

| Level | Meaning  |
|-------|----------|
| 0     | Off      |
| 6     | Enabled  |
| 12    | Cursor   |
| 15    | Playhead |

Overlay priority:

`Playhead > Cursor > Enabled > Off`

Extending the system
--------------------

Planned / easy extensions:

- Paging for >128 NT steps
- Probability visualization via brightness scaling
- Multiple UI modes (edit/play)
- MIDI clock sync
- Bidirectional state refresh
- Robust hot-reconnect handling

