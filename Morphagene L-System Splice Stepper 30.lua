-- Rewritten for:
--   - cleaner state model
--   - semantic UI rendering
--   - USB MIDI semantic mirroring for Raspberry Pi / monome Grid
-- Compatible with Monome Grid Bridge (Disting NT ↔ Grid). Grid key presses
-- are sent to the NT as MIDI notes; this script toggles steps on Note On.
-- See Disting NT User Manual & Lua Scripting docs (Expert Sleepers).
-- OUT1: Morphagene ORGANIZE CV (0..5V, bin-centered for N splices)
-- OUT2: 5V pulse to Morphagene PLAY
-- MIDI semantic mirror (USB):
--   CC10 = cursor index on current page (0..127)
--   CC11 = current page (1-based)
--   CC12 value 0 = clear playhead (when sequencer stopped; avoids stuck box 00)
--   Note On note=<page-local index>, vel=20   -> enabled
--   Note On note=<page-local index>, vel=0    -> disabled
--   Note On note=<page-local index>, vel=100  -> playhead
-- Incoming MIDI (from Grid via bridge): Note On note=page-local index, vel=127
--   toggles the step at that index on the current page.
-- Grid/Pi side should treat the current page as a 16x8 local window.

-- Fixed maximum number of splices. All indexing and iteration must respect 1..96.
local MAX_SPLICES   = 96
local PAGE_CAPACITY = 128
local GRID_COLS     = 16
local GRID_ROWS     = 8

local OUTPUT_BUFFER = {0.0, 0.0}

-- Semantic UI states
local STATE_EMPTY      = 0
local STATE_EXIST      = 1
local STATE_ENABLED    = 2
local STATE_SELECTED   = 3
local STATE_PLAYHEAD   = 4
local STATE_TRIGGERED  = 5

-- USB MIDI destination for sendMIDI(where, ...). Manual: 0x4 = USB MIDI.
local MIDI_DEST_USB = 0x4

local mg = {
    nSplices = 20,
    page = 1,
    cursorIndex = 1,

    active = {},
    enabled = {},
    enabledCount = 0,

    pos = 0,                -- position within enabled[]; 0 = not started
    cachedCV = 0.0,

    pulseRemaining = 0.0,   -- seconds
    triggerFlash = 0.0,     -- seconds, for UI highlight

    cellState = {},

    needsMidiFullSync = true,
    lastPlayheadIndex = 0,
    lastCursorIndex = 0,
    lastPage = 1,
}

for i = 1, MAX_SPLICES do
    mg.active[i] = false
    mg.cellState[i] = STATE_EMPTY
end

-- Utility

local function clamp(x, lo, hi)
    if x < lo then return lo end
    if x > hi then return hi end
    return x
end

local function wrap(x, lo, hi)
    local span = hi - lo + 1
    while x < lo do x = x + span end
    while x > hi do x = x - span end
    return x
end

local function pageCountForN(n)
    return math.max(1, math.ceil(n / PAGE_CAPACITY))
end

local function pageStartFor(page)
    return ((page - 1) * PAGE_CAPACITY) + 1
end

local function pageEndFor(page, n)
    return math.min(n, pageStartFor(page) + PAGE_CAPACITY - 1)
end

local function currentPageRange()
    return pageStartFor(mg.page), pageEndFor(mg.page, mg.nSplices)
end

local function isOnCurrentPage(absIndex)
    local s, e = currentPageRange()
    return absIndex >= s and absIndex <= e
end

local function absToPageLocal(absIndex)
    local s = pageStartFor(mg.page)
    return absIndex - s -- 0..127
end

local function pageLocalToAbs(localIndex)
    local s = pageStartFor(mg.page)
    return s + localIndex
end

-- Splice index → ORGANIZE CV (0–5 V). Morphagene expects 0–5 V; each splice is one bin.
-- Center-of-bin: splice i (1..N) → voltage = (i - 0.5) * (5/N) so Morphagene reliably selects that splice.
-- Example: N=20 → bin=0.25 V; splice 1→0.125 V, splice 10→2.375 V, splice 20→4.875 V.
local function spliceIndexToVoltage(i)
    local N = mg.nSplices
    if N <= 0 then return 0.0 end
    i = clamp(i, 1, N)
    local bin = 5.0 / N
    local v = (i - 0.5) * bin
    return clamp(v, 0.0, 5.0)
end

-- MIDI helpers
-- Disting NT Lua: sendMIDI(where, byte1, byte2, byte3). where: 0x4 = USB.
-- Status byte = base + (channel - 1); channel from parameters[4] is 1-16.
-- Note On base 0x90, Note Off 0x80, Control Change 0xB0.

local function midiStatus(base, ch1to16)
    local ch = clamp(ch1to16 or 1, 1, 16) - 1
    return base + ch
end

local function sendNoteOn(ch, note, vel)
    note = clamp(note or 0, 0, 127)
    vel  = clamp(vel or 0, 0, 127)
    sendMIDI(MIDI_DEST_USB, midiStatus(0x90, ch), note, vel)
end

local function sendCC(ch, cc, val)
    cc  = clamp(cc or 0, 0, 127)
    val = clamp(val or 0, 0, 127)
    sendMIDI(MIDI_DEST_USB, midiStatus(0xB0, ch), cc, val)
end

local function midiChannel(self)
    return self.parameters[4]
end

local function sendCursor(self)
    local ch = midiChannel(self)
    if isOnCurrentPage(mg.cursorIndex) then
        sendCC(ch, 10, absToPageLocal(mg.cursorIndex))
    end
end

local function sendPage(self)
    local ch = midiChannel(self)
    sendCC(ch, 11, clamp(mg.page, 1, 127))
end

local function sendEnableStateForAbs(self, absIndex)
    local ch = midiChannel(self)
    if not isOnCurrentPage(absIndex) then return end

    local note = absToPageLocal(absIndex)
    if mg.active[absIndex] then
        sendNoteOn(ch, note, 20)  -- semantic: enabled
    else
        sendNoteOn(ch, note, 0)   -- semantic: disabled
    end
end

local function sendPlayhead(self, absIndex)
    local ch = midiChannel(self)
    if not absIndex or absIndex <= 0 then return end
    if not isOnCurrentPage(absIndex) then return end

    local note = absToPageLocal(absIndex)
    sendNoteOn(ch, note, 100)     -- semantic: playhead
end

-- Tell grid bridge to clear playhead overlay (when sequencer is stopped).
local function sendPlayheadClear(self)
    local ch = midiChannel(self)
    sendCC(ch, 12, 0)
end

local function sendFullPageState(self)
    sendPage(self)
    sendCursor(self)

    local s, e = currentPageRange()
    for i = s, e do
        sendEnableStateForAbs(self, i)
    end

    if mg.pos ~= 0 and mg.enabledCount > 0 then
        local playSplice = mg.enabled[mg.pos]
        if playSplice then
            sendPlayhead(self, playSplice)
        end
    else
        sendPlayheadClear(self)
    end

    mg.needsMidiFullSync = false
end

-- Enabled list + cursor/playhead helpers

local function rebuildEnabledList()
    mg.enabledCount = 0
    for i = 1, mg.nSplices do
        if mg.active[i] then
            mg.enabledCount = mg.enabledCount + 1
            mg.enabled[mg.enabledCount] = i
        end
    end

    for i = mg.enabledCount + 1, #mg.enabled do
        mg.enabled[i] = nil
    end
end

local function findCursorPosInEnabled()
    if mg.enabledCount <= 0 then return 0 end
    local cur = mg.cursorIndex
    for p = 1, mg.enabledCount do
        if mg.enabled[p] == cur then
            return p
        end
    end
    return 0
end

local function recomputeAndClampParams(self)
    mg.nSplices = clamp(self.parameters[1], 1, MAX_SPLICES)

    local maxPage = pageCountForN(mg.nSplices)
    local p = clamp(self.parameters[3], 1, maxPage)
    mg.page = p

    if p ~= self.parameters[3] then
        setParameter(self.algorithmIndex, self.parameterOffset + 3, p, true)
    end

    mg.cursorIndex = clamp(mg.cursorIndex, 1, mg.nSplices)
end

local function moveCursorToPageStart()
    local s = pageStartFor(mg.page)
    mg.cursorIndex = clamp(s, 1, mg.nSplices)
end

-- When (re)starting the sequencer from a stopped state, always begin from the
-- lowest-numbered active splice (splice 1 if active, otherwise 2, 3, ...).
-- Returns true if a valid starting splice was found, false if there are none.
local function startFromFirstActive()
    rebuildEnabledList()
    if mg.enabledCount <= 0 then
        mg.pos = 0
        mg.cachedCV = 0.0
        return false
    end

    mg.pos = 1
    local spliceIndex = mg.enabled[mg.pos]
    mg.cachedCV = spliceIndexToVoltage(spliceIndex)
    return true
end

-- After any change to mg.active (enabling/disabling splices), ensure that:
-- - the enabled list is up to date,
-- - the sequencer is stopped if there are no active splices,
-- - otherwise the playhead points at a valid active splice (the first active),
-- - ORGANIZE CV and grid state will be refreshed on the next draw.
local function ensureValidPlayhead(self)
    rebuildEnabledList()

    if mg.enabledCount <= 0 then
        mg.pos = 0
        mg.cachedCV = 0.0
        -- Explicitly clear playhead overlay on the grid when nothing is active.
        sendPlayheadClear(self)
        mg.needsMidiFullSync = true
        return
    end

    local currentSplice = nil
    if mg.pos ~= 0 and mg.enabledCount > 0 and mg.enabled[mg.pos] then
        currentSplice = mg.enabled[mg.pos]
    end

    if mg.pos == 0 or not currentSplice or not mg.active[currentSplice] then
        mg.pos = 1
        currentSplice = mg.enabled[1]
    end

    mg.cachedCV = spliceIndexToVoltage(currentSplice)
    mg.needsMidiFullSync = true
end

-- L-system-like weighted motion

local STEP_WEIGHTS = {
    [-2] = 1,
    [-1] = 3,
    [ 0] = 4,
    [ 1] = 3,
    [ 2] = 1
}

local function weightedChoice(tbl)
    local total = 0
    for _, w in pairs(tbl) do
        total = total + w
    end

    local r = math.random() * total
    local acc = 0
    for k, w in pairs(tbl) do
        acc = acc + w
        if r <= acc then
            return k
        end
    end
    return 0
end

local function nextMove()
    return weightedChoice(STEP_WEIGHTS)
end

-- Semantic UI state

local function clearCellStates()
    for i = 1, mg.nSplices do
        mg.cellState[i] = STATE_EXIST
    end
    for i = mg.nSplices + 1, MAX_SPLICES do
        mg.cellState[i] = STATE_EMPTY
    end
end

local function updateCellStates()
    clearCellStates()

    -- Base enabled state
    for i = 1, mg.nSplices do
        if mg.active[i] then
            mg.cellState[i] = STATE_ENABLED
        end
    end

    -- Cursor
    if mg.cursorIndex >= 1 and mg.cursorIndex <= mg.nSplices then
        mg.cellState[mg.cursorIndex] = STATE_SELECTED
    end

    -- Playhead
    if mg.pos ~= 0 and mg.enabledCount > 0 then
        local playSplice = mg.enabled[mg.pos]
        if playSplice then
            mg.cellState[playSplice] = STATE_PLAYHEAD
        end
    end

    -- Trigger flash overrides playhead briefly
    if mg.triggerFlash > 0.0 and mg.pos ~= 0 and mg.enabledCount > 0 then
        local playSplice = mg.enabled[mg.pos]
        if playSplice then
            mg.cellState[playSplice] = STATE_TRIGGERED
        end
    end
end

local function renderCellState(state, x1, y1, x2, y2)
    if state == STATE_EMPTY then
        -- draw nothing
        return

    elseif state == STATE_EXIST then
        drawBox(x1, y1, x2, y2, 4)

    elseif state == STATE_ENABLED then
        drawBox(x1, y1, x2, y2, 5)
        drawRectangle(x1 + 1, y1 + 1, x2 - 1, y2 - 1, 6)

    elseif state == STATE_SELECTED then
        drawBox(x1, y1, x2, y2, 6)
        drawBox(x1 - 1, y1 - 1, x2 + 1, y2 + 1, 15)

    elseif state == STATE_PLAYHEAD then
        drawBox(x1, y1, x2, y2, 6)
        drawRectangle(x1 + 1, y1 + 1, x2 - 1, y2 - 1, 15)

    elseif state == STATE_TRIGGERED then
        drawBox(x1, y1, x2, y2, 6)
        drawRectangle(x1 + 1, y1 + 1, x2 - 1, y2 - 1, 12)
        drawBox(x1 - 1, y1 - 1, x2 + 1, y2 + 1, 15)
    end
end

-- Main algorithm

return {
    name   = "MG L-System Splice Stepper 300",
    author = "OpenAI / revised",

    init = function(self)
        for i = 1, MAX_SPLICES do
            mg.active[i] = false
            mg.cellState[i] = STATE_EMPTY
        end

        mg.nSplices = 20
        mg.page = 1
        mg.cursorIndex = 1
        mg.pos = 0
        mg.cachedCV = spliceIndexToVoltage(1)
        mg.pulseRemaining = 0.0
        mg.triggerFlash = 0.0
        mg.enabledCount = 0
        mg.needsMidiFullSync = true
        mg.lastPlayheadIndex = 0
        mg.lastCursorIndex = 0
        mg.lastPage = 1

        rebuildEnabledList()
        updateCellStates()

        return {
            inputs = { kTrigger },
            outputs = { kLinear, kGate },
            inputNames = { "Step Trig" },
            outputNames = { "Organize CV", "Play Pulse" },

            parameters = {
                { "Splices", 1, MAX_SPLICES, 20, kInt },
                { "Pulse ms", 1, 1000, 20, kMs },
                { "Page", 1, pageCountForN(MAX_SPLICES), 1, kInt },
                { "MIDI ch", 1, 16, 1, kInt },
            },

            -- Receive grid key presses from Monome Grid Bridge (USB MIDI Note On).
            midi = { channelParameter = 4, messages = { "note" } },
        }
    end,

    ui = function(self)
        return true
    end,

    trigger = function(self, input)
        if input ~= 1 then return end

        recomputeAndClampParams(self)
        rebuildEnabledList()

        if mg.enabledCount <= 0 then
            -- No active splices at all: stop and clear playhead.
            sendPlayheadClear(self)
            return
        end

        if mg.pos == 0 then
            -- First step after stop: start from the lowest-numbered active splice.
            local ok = startFromFirstActive()
            if not ok then
                sendPlayheadClear(self)
                return
            end

            mg.pulseRemaining = clamp(self.parameters[2], 1, 1000) / 1000.0
            mg.triggerFlash = 0.08

            local playSplice = mg.enabled[mg.pos]
            sendPlayhead(self, playSplice)
            return
        end

        local move = nextMove()
        local newPos = wrap(mg.pos + move, 1, mg.enabledCount)
        mg.pos = newPos

        local spliceIndex = mg.enabled[mg.pos]
        mg.cachedCV = spliceIndexToVoltage(spliceIndex)

        mg.pulseRemaining = clamp(self.parameters[2], 1, 1000) / 1000.0
        mg.triggerFlash = 0.08

        sendPlayhead(self, spliceIndex)
    end,

    -- Grid key presses from Monome Grid Bridge arrive as USB MIDI Note On
    -- (note = page-local index 0..127, vel = 127). Toggle the step at that index.
    midiMessage = function(self, msg)
        local status, note, vel = msg[1], msg[2], msg[3]
        -- Note On: status 0x90..0x9F, velocity > 0
        if not status or status < 0x90 or status > 0x9F or not vel or vel == 0 then
            return
        end
        recomputeAndClampParams(self)
        local absIndex = pageLocalToAbs(note)
        if absIndex < 1 or absIndex > mg.nSplices then
            return
        end
        mg.active[absIndex] = not mg.active[absIndex]
        sendEnableStateForAbs(self, absIndex)
        -- Any grid edit must immediately reflect in enabled list and playhead.
        ensureValidPlayhead(self)
    end,

    step = function(self, dt, inputs)
        -- Output CV
        OUTPUT_BUFFER[1] = mg.cachedCV

        -- Gate / pulse
        if mg.pulseRemaining > 0.0 then
            mg.pulseRemaining = mg.pulseRemaining - dt
            if mg.pulseRemaining < 0.0 then
                mg.pulseRemaining = 0.0
            end
            OUTPUT_BUFFER[2] = 5.0
        else
            OUTPUT_BUFFER[2] = 0.0
        end

        -- UI trigger flash timer
        if mg.triggerFlash > 0.0 then
            mg.triggerFlash = mg.triggerFlash - dt
            if mg.triggerFlash < 0.0 then
                mg.triggerFlash = 0.0
            end
        end

        return OUTPUT_BUFFER
    end,

    -- UI editing

    encoder1Turn = function(self, dir)
        recomputeAndClampParams(self)

        local s, e = currentPageRange()
        local idx = clamp(mg.cursorIndex, s, e)
        idx = idx + (dir or 0)

        if idx < s then idx = e end
        if idx > e then idx = s end

        mg.cursorIndex = idx
        sendCursor(self)
    end,

    encoder2Turn = function(self, dir)
        recomputeAndClampParams(self)

        local maxPage = pageCountForN(mg.nSplices)
        local p = clamp(mg.page + (dir or 0), 1, maxPage)

        if p ~= mg.page then
            mg.page = p
            setParameter(self.algorithmIndex, self.parameterOffset + 3, p, true)
            moveCursorToPageStart()
            mg.needsMidiFullSync = true
            sendPage(self)
            sendCursor(self)
        end
    end,

    encoder3Turn = function(self, dir)
        local n = clamp(self.parameters[1] + (dir or 0), 1, MAX_SPLICES)
        setParameter(self.algorithmIndex, self.parameterOffset + 1, n, true)

        recomputeAndClampParams(self)

        if mg.cursorIndex > mg.nSplices then
            mg.cursorIndex = mg.nSplices
        end

        -- Changing the total number of splices should rebuild the enabled list
        -- and keep the playhead on a valid active splice (or stop if none).
        ensureValidPlayhead(self)
    end,

    button1Push = function(self)
        recomputeAndClampParams(self)

        local i = clamp(mg.cursorIndex, 1, mg.nSplices)
        mg.active[i] = not mg.active[i]
        sendEnableStateForAbs(self, i)
        -- Button edits share the same rules as grid edits.
        ensureValidPlayhead(self)
    end,

    button2Push = function(self)
        recomputeAndClampParams(self)

        for i = 1, mg.nSplices do
            mg.active[i] = false
        end
        -- All off → sequencer off and playhead cleared.
        ensureValidPlayhead(self)
    end,

    button3Push = function(self)
        recomputeAndClampParams(self)

        for i = 1, mg.nSplices do
            mg.active[i] = true
        end
        -- All on → start from the first splice on next trigger.
        ensureValidPlayhead(self)
    end,

    button4Push = function(self)
        mg.pos = 0
        mg.needsMidiFullSync = true
    end,

    -- Draw

    draw = function(self)
        recomputeAndClampParams(self)
        rebuildEnabledList()
        updateCellStates()

        if mg.needsMidiFullSync then
            sendFullPageState(self)
        end

        local s, e = currentPageRange()
        local countThisPage = e - s + 1
        local maxPage = pageCountForN(mg.nSplices)

        -- Header
        drawText(2, 2, "N=" .. mg.nSplices .. "  Pg " .. mg.page .. "/" .. maxPage)
        drawText(120, 2, "Cur=" .. mg.cursorIndex)
        drawText(190, 2, "En=" .. mg.enabledCount)

        local playSplice = 0
        if mg.pos ~= 0 and mg.enabledCount > 0 then
            playSplice = mg.enabled[mg.pos] or 0
        end
        drawText(2, 11, "Play=" .. playSplice .. "  CV=" .. string.format("%.3f", mg.cachedCV))

        -- 16x8 fixed page grid
        local ox = 0
        local oy = 18
        local boxW = 16
        local boxH = 8

        for localIdx = 0, (PAGE_CAPACITY - 1) do
            local absIndex = pageLocalToAbs(localIdx)

            local col = localIdx % GRID_COLS
            local row = math.floor(localIdx / GRID_COLS)

            local x1 = ox + (col * boxW)
            local y1 = oy + (row * boxH)
            local x2 = x1 + boxW - 2
            local y2 = y1 + boxH - 2

            if absIndex <= e then
                local state = mg.cellState[absIndex]
                renderCellState(state, x1, y1, x2, y2)
            end
        end
    end
}