"""Lua snippets that run inside a vehicle's Lua VM, shared by the backends."""

from __future__ import annotations

GEAR_TARGETS = ["park", "reverse", "neutral", "drive", "up", "down", "1", "2", "3", "4", "5", "6", "7", "8"]

# Automatic-style gearboxes (automatic, dct, cvt, electricMotor) map H-shifter
# indices to modes: R=-1, N=0, P=1, D=2, S=3, "2"=4, "1"=5, M1=6.
# Manual and sequential gearboxes use the real gear index (R=-1, N=0, 1..n).
_SHIFT = """
local mc = controller.mainController
local gb = powertrain.getDevice("gearbox")
local kind = gb and gb.type or ""
local target = %s
if target == "up" then mc.shiftUpOnDown() mc.shiftUpOnUp() return end
if target == "down" then mc.shiftDownOnDown() mc.shiftDownOnUp() return end
local n = tonumber(target)
if kind == "manualGearbox" or kind == "sequentialGearbox" then
  local idx = ({reverse = -1, neutral = 0, park = 0, drive = 1})[target] or n
  mc.shiftToGearIndex(idx)
  if target == "park" then input.event('parkingbrake', 1, 2) end
  return
end
-- Automatic-style: not every car has every selector position (a rock crawler may only have P R N M).
local vc = v and v.data and v.data.vehicleController or {}
local modes = vc.automaticModes or "PRNDS21M"
local has = function(m) return modes:find(m, 1, true) ~= nil end
if n then
  if has("M") then
    mc.shiftToGearIndex(6)
    for _ = 2, n do mc.shiftUpOnDown() mc.shiftUpOnUp() end
  else
    mc.shiftToGearIndex(2)
  end
elseif target == "drive" then
  mc.shiftToGearIndex(has("D") and 2 or has("S") and 3 or 6)
elseif target == "park" then
  if has("P") then mc.shiftToGearIndex(1) else mc.shiftToGearIndex(0); input.event('parkingbrake', 1, 2) end
else
  mc.shiftToGearIndex(({reverse = -1, neutral = 0})[target])
end
"""


def shift(target: str) -> str:
    assert target in GEAR_TARGETS
    return _SHIFT % repr(target)


def gearbox_mode(mode: str) -> str:
    assert mode in ("arcade", "realistic")
    return f"if controller.mainController then controller.mainController.setGearboxMode('{mode}') end"


# Ignition levels work like the key: 0 off, 1 accessory, 2 on, 3 cranking. The game drops
# 3 back to 2 by itself once the engine fires.
CRANK = "electrics.setIgnitionLevel(3)"
STOP_CRANKING = "if electrics.values.ignitionLevel == 3 then electrics.setIgnitionLevel(2) end"
IGNITION_OFF = "electrics.setIgnitionLevel(0)"
