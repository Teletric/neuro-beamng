-- Helper installed into BeamNG's game-engine Lua VM through the MCP run_lua tool.
-- Everything targets the current player vehicle, so it follows car swaps.

neuroBridge = neuroBridge or {}
local B = neuroBridge
B.electrics = B.electrics or {}

-- Runs in the vehicle VM; sends a few electrics values back to GE. Read on the next poll.
local ELECTRICS_CMD = [[
local e = electrics.values
local gb = powertrain.getDevice("gearbox")
local vc = v and v.data and v.data.vehicleController or {}
local combustion, motors = false, false -- electric = motors and no combustion engine
for _, d in pairs(powertrain.getDevices() or {}) do
  if d.type == "combustionEngine" then combustion = true elseif d.type == "electricMotor" then motors = true end
end
obj:queueGameEngineLua(string.format("if neuroBridge then neuroBridge.electrics[%d] = jsonDecode(%q) end",
  obj:getId(), jsonEncode({gear = e.gear, rpm = e.rpm, wheelspeed = e.wheelspeed, fuel = e.fuel,
    engineRunning = e.engineRunning, gearboxMode = e.gearboxMode, electric = motors and not combustion,
    gearboxType = gb and gb.type, autoModes = vc.automaticModes,
    minGear = gb and gb.minGearIndex, maxGear = gb and gb.maxGearIndex})))
]]

-- Installed in the player car's Lua VM (idempotent): forwards the car's on-screen messages and its
-- damage tracker updates to GE. Vehicle messages go straight to the UI, so GE never sees them otherwise.
local VEHICLE_HOOK_CMD = [[
if __neuroHooked then return end
__neuroHooked = true
local function fwd(kind, payload)
  obj:queueGameEngineLua(string.format("if neuroBridge then neuroBridge.onVehicleEvent(%d, %q) end",
    obj:getId(), jsonEncode({kind = kind, data = payload})))
end
local function wrap(fn)
  return function(msg, ttl, category, ...)
    pcall(fwd, 'message', {msg = msg, category = category})
    return fn(msg, ttl, category, ...)
  end
end
guihooks.message = wrap(guihooks.message)
guihooks.messageForSeatedPlayers = wrap(guihooks.messageForSeatedPlayers)
if damageTracker and damageTracker.registerDamageUpdateCallback then
  damageTracker.registerDamageUpdateCallback(function(data) pcall(fwd, 'damage', data) end)
end
]]

B.events = B.events or {}
B.damage = B.damage or {}

-- Readable text from a UI message: plain string, translation key, or {txt=key, context={...}}.
local function messageText(msg)
  local txt, ctx = msg, nil
  if type(msg) == 'table' then
    if msg.msg ~= nil then msg = msg.msg end
    txt, ctx = msg, nil
    if type(msg) == 'table' then txt, ctx = msg.txt, msg.context end
  end
  if type(txt) ~= 'string' or txt == '' then return nil end
  local s = translateLanguage(txt, txt)
  -- Fill {{key}} / {{key | translate}} placeholders from the context, translating values.
  local function text(v)
    if type(v) == 'table' then v = v.txt or v.msg or v.text end -- nested {txt=..., context=...}
    if type(v) == 'string' then return translateLanguage(v, v) end
    if type(v) == 'number' or type(v) == 'boolean' then return tostring(v) end
    return ''
  end
  s = s:gsub('{{%s*([%w_%.]+)%s*[^}]*}}', function(k)
    return type(ctx) == 'table' and text(ctx[k]) or ''
  end)
  if s:find('table: 0x') then return nil end -- something we couldn't unpack
  -- Drop key-binding hints like "([action=toggleESCMode] to change)".
  s = s:gsub('%s*%(%[action=[^%]]*%][^)]*%)', ''):gsub('%[action=[^%]]*%]', '')
  if not s:find('%a') then return nil end -- nothing left worth saying
  return s
end

function B.onMessage(msg, category, source)
  local ok, text = pcall(messageText, msg)
  if not ok or not text then return end
  if type(msg) == 'table' and msg.category and not category then category = msg.category end
  B.events[#B.events + 1] = {text = text, category = tostring(category or ''), source = source}
  while #B.events > 50 do table.remove(B.events, 1) end
end

function B.onVehicleEvent(vid, json)
  if vid ~= be:getPlayerVehicleID(0) then return end
  local ok, ev = pcall(jsonDecode, json)
  if not ok or type(ev) ~= 'table' then return end
  if ev.kind == 'message' and ev.data then
    B.onMessage(ev.data.msg, ev.data.category, 'car')
  elseif ev.kind == 'damage' then
    B.damage[vid] = ev.data
  end
end

-- Messages raised in GE itself (police, traffic, gameplay). Wrapped once; B survives re-installs.
if not B.geHooked then
  B.geHooked = true
  for _, name in ipairs({'message', 'messageForSeatedPlayers'}) do
    local orig = guihooks[name]
    guihooks[name] = function(msg, ttl, category, ...)
      if neuroBridge then pcall(neuroBridge.onMessage, msg, category, 'game') end
      return orig(msg, ttl, category, ...)
    end
  end
end

-- Active problems from the damage tracker, as readable text.
local function problems(vid)
  local out = {}
  for group, names in pairs(B.damage[vid] or {}) do
    if type(names) == 'table' then
      for name, val in pairs(names) do
        if val == true or (type(val) == 'number' and val > 0) then
          local key = string.format('vehicle.%s.%s.true', group, name)
          local text = translateLanguage(key, key)
          out[#out + 1] = text ~= key and text or (group .. ': ' .. name)
        end
      end
    end
  end
  table.sort(out)
  return out
end

-- Police / pursuit state of the player car (only while the traffic system is running).
local function policeStatus(id)
  if not (gameplay_traffic and gameplay_traffic.getTrafficData) then return nil end
  local td = gameplay_traffic.getTrafficData()
  local me = td and td[id]
  if not me then return nil end
  local out = {role = me.roleName, mode = me.pursuit and me.pursuit.mode or 0, police = 0}
  local mine = map.objects[id]
  if gameplay_police and gameplay_police.getPoliceVehicles then
    for pid in pairs(gameplay_police.getPoliceVehicles() or {}) do
      local po = map.objects[pid]
      if pid ~= id and po then
        out.police = out.police + 1
        local d = mine and (po.pos - mine.pos):length() or 1e9
        if not out.nearest or d < out.nearest.dist then
          out.nearest = {vid = pid, dist = d, pos = {po.pos.x, po.pos.y, po.pos.z}}
        end
      end
    end
  end
  if me.roleName == 'police' and me.role and me.role.targetId then
    local target = getObjectByID(me.role.targetId)
    local o2 = map.objects[me.role.targetId]
    if target and o2 then
      out.suspect = {vid = me.role.targetId, name = target:getJBeamFilename(), pos = {o2.pos.x, o2.pos.y, o2.pos.z},
                     chasing = (me.role.targetPursuitMode or 0) > 0}
    end
  end
  return out
end

local function player()
  local id = be:getPlayerVehicleID(0)
  if not id or id < 0 then return nil end
  return id, getObjectByID(id)
end

local function v3(v) return {v.x, v.y, v.z} end

function B.poll()
  local id, veh = player()
  if not veh then return jsonEncode({error = "no player vehicle"}) end
  be:queueObjectLua(id, ELECTRICS_CMD)
  be:queueObjectLua(id, VEHICLE_HOOK_CMD) -- no-op once installed; re-installs after a car swap or reload
  if B.ensurePilotHook then B.ensurePilotHook() end
  local o = map.objects[id]
  if o then B.lastPos = {vid = id, pos = vec3(o.pos)} end -- where the car was before any reset (a copy: o.pos is live)
  local e = B.electrics[id] or {}
  local out = {
    vid = id,
    model = veh:getJBeamFilename(),
    config = (veh.partConfig or ''):match('([^/]+)%.pc$'),
    level = getCurrentLevelIdentifier(),
    driveLeft = (function() local ok, r = pcall(map.getRoadRules) return ok and r and r.rightHandDrive or nil end)(),
    gear = e.gear, rpm = e.rpm, wheelspeed = e.wheelspeed, fuel = e.fuel,
    engineRunning = e.engineRunning, gearboxMode = e.gearboxMode, electric = e.electric,
    gearboxType = e.gearboxType, autoModes = e.autoModes, maxGear = e.maxGear,
    paused = simTimeAuthority and simTimeAuthority.getPause and simTimeAuthority.getPause() or false,
    problems = problems(id),
    police = policeStatus(id),
    pilot = B.pilotStatus and B.pilotStatus() or nil,
  }
  if #B.events > 0 then
    out.events = B.events
    B.events = {}
  end
  if o then
    out.pos, out.dir, out.up = v3(o.pos), v3(o.dirVec), v3(o.dirVecUp)
    out.speed = o.vel:length()
    out.fwdSpeed = o.vel:dot(o.dirVec) -- negative while rolling backwards
    out.damage = o.damage or 0
  else
    out.pos, out.dir, out.up = v3(veh:getPosition()), v3(veh:getDirectionVector()), v3(veh:getDirectionVectorUp())
  end
  return jsonEncode(out)
end

function B.nearby(radius)
  local id = player()
  local me = id and map.objects[id]
  if not me then return "[]" end
  local out = {}
  for oid, o in pairs(map.objects) do
    if oid ~= id then
      local d = (o.pos - me.pos):length()
      if d <= radius then
        local ob = getObjectByID(oid)
        out[#out + 1] = {vid = oid, model = ob and ob:getJBeamFilename() or "car", pos = v3(o.pos)}
      end
    end
  end
  if #out == 0 then return "[]" end
  return jsonEncode(out)
end

-- The navgraph stores each road segment on only one of its two nodes; build a two-way
-- adjacency once per map.
local function adjacency(nodes)
  -- The game reuses the same nodes table for a new level, so key the cache on the level too.
  local level = getCurrentLevelIdentifier()
  if B.adj and B.adjNodes == nodes and B.adjLevel == level then return B.adj end
  B.adjLevel = level
  local adj = {}
  for name, n in pairs(nodes) do
    adj[name] = adj[name] or {}
    for to in pairs(n.links or {}) do
      adj[name][to] = true
      adj[to] = adj[to] or {}
      adj[to][name] = true
    end
  end
  B.adj, B.adjNodes = adj, nodes
  return adj
end

-- Follow the road network forward from the player car, always taking the straightest
-- branch. Returns the distance to the nearest road, its half-width, and points along
-- the way as {x, y, isJunction}.
function B.roadAhead(maxDist)
  local id = player()
  local me = id and map.objects[id]
  if not me then return jsonEncode({error = "no player vehicle"}) end
  local nodes = map.getMap().nodes
  local adj = adjacency(nodes)
  local n1, n2, dist = map.findClosestRoad(me.pos)
  if not n1 or not nodes[n1] or not nodes[n2] then return jsonEncode({noRoads = true}) end

  local cur, prev = n2, n1
  if (nodes[n2].pos - nodes[n1].pos):dot(me.dirVec) < 0 then cur, prev = n1, n2 end
  local heading = (nodes[cur].pos - nodes[prev].pos):normalized()
  local pts, total, last = {}, 0, me.pos
  for _ = 1, 60 do
    local p = nodes[cur].pos
    total = total + (p - last):length()
    last = p
    local links, count = adj[cur] or {}, 0
    for _ in pairs(links) do count = count + 1 end
    pts[#pts + 1] = {p.x, p.y, count > 2 and 1 or 0}
    if total >= maxDist then break end
    local best, bestDot
    for to in pairs(links) do
      if to ~= prev and nodes[to] then
        local d = (nodes[to].pos - p):normalized():dot(heading)
        if not bestDot or d > bestDot then best, bestDot = to, d end
      end
    end
    if not best then break end
    heading = (nodes[best].pos - p):normalized()
    prev, cur = cur, best
  end
  return jsonEncode({dist = dist, halfWidth = nodes[n1].radius or 4, points = pts})
end

-- Simulated lidar: a level fan of rays around the car against the world's static geometry
-- and terrain. Each hit reports angle (degrees clockwise from the nose), distance, the
-- surface normal (tells ramps from walls), height relative to the car, and what was hit.
-- Vegetation is one big "Forest" object; its individual plants are found by position.
local function forestItems(pt, radius)
  local forest = core_forest and core_forest.getForestObject and core_forest.getForestObject()
  if not forest then return {} end
  local ok, items = pcall(function() return forest:getData():getItemsCircle(pt, radius) end)
  return ok and items or {}
end

local function hitInfo(obj, pt)
  local cls, shape = '', ''
  if obj then
    pcall(function() cls = obj:getClassName() or '' end)
    pcall(function() shape = tostring(obj.shapeName or '') end)
  end
  if cls == 'Forest' and pt then -- name the plant that was hit (shrub, aspen, fir...)
    local best, bestD
    for _, item in ipairs(forestItems(pt, 4)) do
      local d = (vec3(item:getPosition()) - pt):length()
      if not bestD or d < bestD then best, bestD = item, d end
    end
    if best then pcall(function() shape = best:getData():getName() end) end
  end
  return cls, (shape:lower():match('([^/]+)%.%w+$') or shape:lower())
end

-- A plant only stops a car at its core (the game's collision radius times the plant's size); the
-- leaves around it are pushed through. Distance along the ray to the first core it would really hit
-- near the leafy point `pt`, or nil if it only brushes leaves.
local function plantCoreHit(origin, dir, pt, reach)
  local flatd = vec3(dir.x, dir.y, 0)
  local len = flatd:length()
  if len < 0.2 then -- (nearly) vertical, like the ground scan: solid only straight over a core
    for _, item in ipairs(forestItems(pt, 6)) do
      local okR, r = pcall(function() return (item:getData().radius or 0.3) * (item:getScale() or 1) end)
      local c = vec3(item:getPosition())
      if vec3(c.x - pt.x, c.y - pt.y, 0):length() < (okR and r or 0.3) then return (pt - origin):length() end
    end
    return nil
  end
  flatd = flatd / len
  local best
  for _, item in ipairs(forestItems(pt, 6)) do
    local okR, r = pcall(function() return (item:getData().radius or 0.3) * (item:getScale() or 1) end)
    r = okR and r or 0.3
    local c = vec3(item:getPosition()) - origin
    local along = c.x * flatd.x + c.y * flatd.y
    local across = math.abs(c.x * flatd.y - c.y * flatd.x)
    if along > 0 and across < r + 0.1 then
      local t = (along - math.sqrt(math.max(0, (r + 0.1) ^ 2 - across ^ 2))) / len
      if t <= reach and (not best or t < best) then best = t end
    end
  end
  return best
end

local PLANT_WORDS = {'tree', 'shrub', 'bush', 'grass', 'fern', 'plant', 'fir', 'pine', 'oak', 'aspen', 'birch',
                     'poplar', 'palm', 'cork', 'cypress', 'willow', 'maple', 'cactus', 'reed', 'hedge'}
local function isPlant(name)
  for _, w in ipairs(PLANT_WORDS) do
    if name:find(w, 1, true) then return true end
  end
  return false
end

-- castRay that sees through leaves: a vegetation hit only counts at a plant's core; otherwise it
-- carries on past the leaves (a few times) to whatever is behind. Returns {pt, norm, obj} like castRay.
local function castSolid(origin, target)
  local dir = target - origin
  local reach = dir:length()
  if reach < 1e-3 then return nil end
  dir = dir / reach
  local from, travelled = origin, 0
  for _ = 1, 6 do
    local res = castRay(from, origin + dir * reach, true, false)
    if not res then return nil end
    local cls, name = hitInfo(res.obj, res.pt)
    if cls ~= 'Forest' or not isPlant(name) then return res end -- rocks, fences, poles: the whole shape is solid
    local core = plantCoreHit(origin, dir, res.pt, reach)
    if core then return {pt = origin + dir * core, norm = vec3(-dir.x, -dir.y, 0), obj = res.obj} end
    travelled = (res.pt - origin):length() + 0.3
    if travelled >= reach then return nil end
    from = origin + dir * travelled
  end
  return nil
end

-- Distance along a ray to an oriented box (centre c, half-axis vectors), or nil (slab method).
local function rayBox(origin, dir, maxDist, c, axes)
  local tmin, tmax = 0, maxDist
  local p = c - origin
  for i = 1, 3 do
    local len = axes[i]:length()
    if len < 1e-4 then return nil end
    local n = axes[i] / len
    local e, fd = n:dot(p), n:dot(dir)
    if math.abs(fd) > 1e-6 then
      local t1, t2 = (e + len) / fd, (e - len) / fd
      if t1 > t2 then t1, t2 = t2, t1 end
      if t1 > tmin then tmin = t1 end
      if t2 < tmax then tmax = t2 end
      if tmin > tmax then return nil end
    elseif math.abs(e) > len then
      return nil
    end
  end
  return tmin
end

B.modelNames = B.modelNames or {}
local function modelName(model)
  if not B.modelNames[model] then
    local m = core_vehicles.getModel(model)
    local info = m and m.model or {}
    B.modelNames[model] = ((info.Brand or '') .. ' ' .. (info.Name or model)):gsub('^%s+', '')
  end
  return B.modelNames[model]
end

-- Other vehicles' live bounding boxes. Static ray casts don't hit vehicles, so beams are also
-- tested against these and the nearer hit wins: a car behind a wall stays hidden, as with a real lidar.
local function nearbyBoxes(id, me, range)
  local boxes = {}
  for oid, o in pairs(map.objects) do
    if oid ~= id and (o.pos - me.pos):length() < range + 15 then
      local veh = getObjectByID(oid)
      if veh and be:getObjectOOBBIsInitialized(oid) then
        boxes[#boxes + 1] = {
          vid = oid,
          c = vec3(be:getObjectOOBBCenterXYZ(oid)),
          axes = {vec3(be:getObjectOOBBHalfAxisXYZ(oid, 0)), vec3(be:getObjectOOBBHalfAxisXYZ(oid, 1)),
                  vec3(be:getObjectOOBBHalfAxisXYZ(oid, 2))},
          name = modelName(veh:getJBeamFilename()),
          vel = o.vel,
        }
      end
    end
  end
  return boxes
end

-- Diagnostic: in a fan around the car, what a plain ray hits versus what counts as solid (see castSolid).
function B.solidCheck(range)
  local o = map.objects[be:getPlayerVehicleID(0)]
  local origin = o.pos + vec3(0, 0, 0.8)
  local out = {}
  for a = 0, 355, 5 do
    local d = vec3(math.sin(math.rad(a)), math.cos(math.rad(a)), 0)
    local r = castRay(origin, origin + d * range, true, false)
    if r then
      local cls, name = hitInfo(r.obj, r.pt)
      local sres = castSolid(origin, origin + d * range)
      local kept = sres and (sres.pt - origin):length() or nil
      out[#out + 1] = {cls = cls, name = name, raw = (r.pt - origin):length(), solid = kept}
    end
  end
  return jsonEncode(out)
end

-- How far a ray from `origin` (inside the car) travels before it leaves the car's own bounding box:
-- subtracted from lidar distances, so they're measured from her body rather than its middle.
local function bodyExit(id, origin, dir)
  if not be:getObjectOOBBIsInitialized(id) then return 0 end
  local c = vec3(be:getObjectOOBBCenterXYZ(id))
  local tmax = math.huge
  for i = 0, 2 do
    local axis = vec3(be:getObjectOOBBHalfAxisXYZ(id, i))
    local len = axis:length()
    if len > 1e-4 then
      local n = axis / len
      local e, fd = n:dot(c - origin), n:dot(dir)
      if math.abs(fd) > 1e-6 then tmax = math.min(tmax, math.max((e + len) / fd, (e - len) / fd)) end
    end
  end
  return tmax == math.huge and 0 or math.max(0, tmax)
end

function B.lidar(range, beams, height)
  local id = player()
  local o = id and map.objects[id]
  if not o then return "[]" end
  local f = vec3(o.dirVec.x, o.dirVec.y, 0)
  if f:length() < 0.01 then f = vec3(0, 1, 0) end
  f = f:normalized()
  local right = vec3(f.y, -f.x, 0)
  local origin = o.pos + vec3(0, 0, height)
  local boxes = nearbyBoxes(id, o, range)
  local out = {}
  for i = 0, beams - 1 do
    local a = 2 * math.pi * i / beams
    local dir = f * math.cos(a) + right * math.sin(a)
    local res = castRay(origin, origin + dir * range, true, false)
    local dist = res and (res.pt - origin):length() or range
    -- Nearest vehicle along the beam, tested at that vehicle's own height so slopes don't hide it.
    local car, carDist
    for _, b in ipairs(boxes) do
      if math.abs(b.c.z - origin.z) < 4 then
        local t = rayBox(vec3(origin.x, origin.y, b.c.z), dir, dist, b.c, b.axes)
        if t and (not carDist or t < carDist) then car, carDist = b, t end
      end
    end
    local body = bodyExit(id, origin, dir)
    if car then
      -- closing speed: positive when the gap along this beam is shrinking
      local closing = (o.vel - car.vel):dot(dir)
      out[#out + 1] = {math.deg(a), math.max(0, carDist - body), -dir.x, -dir.y, 0, car.c.z - o.pos.z,
                       'BeamNGVehicle', car.name, dir.x, dir.y, closing}
    elseif res then
      local cls, shape = hitInfo(res.obj, res.pt)
      out[#out + 1] = {math.deg(a), math.max(0, dist - body), res.norm.x, res.norm.y, res.norm.z,
                       res.pt.z - o.pos.z, cls, shape, dir.x, dir.y, 0}
    end
  end
  if #out == 0 then return "[]" end
  return jsonEncode(out)
end

-- Screen capture for the vision model. Capture is asynchronous: start it, then fetch a few
-- frames later. The file is deleted once read so nothing piles up in the user folder.
local SHOT_DIR = "screenshots/neuro/"
B.shotCounter = B.shotCounter or 0

function B.captureStart(scale)
  if not FS:directoryExists(SHOT_DIR) then FS:directoryCreate(SHOT_DIR, true) end
  B.shotCounter = B.shotCounter + 1
  local rel = SHOT_DIR .. "frame_" .. B.shotCounter
  local id = require('screenshot').createScreenshotTracked({filename = rel, writeJPG = true, superSampling = 1, rescaleFactor = scale})
  if not id or id == 0 then error("screenshot failed") end
  B.pendingShot = rel .. ".jpg"
  return "ok"
end

-- Base64 JPEG once the capture is fully written, else "".
function B.captureFetch()
  local p = B.pendingShot
  if not p then return "" end
  local f = io.open(p, "rb")
  if not f then return "" end
  local bytes = f:read("*all")
  f:close()
  if #bytes < 4 or bytes:sub(-2) ~= "\255\217" then return "" end -- JPEG end marker not written yet
  B.pendingShot = nil
  pcall(function() FS:removeFile(p) end)
  return require('mime').b64(bytes)
end

-- Vehicle-specific controls (doors, lift axles, retarder, hydraulic PTO, lowrider hops, ...).
-- Same sources the game's input system uses: the vehicle's jbeam inputActions plus any
-- input_actions*.json, filtered to the actions the fitted parts enable. Computed directly
-- because core_input_actions caches its list and goes stale when the player switches cars.
local function vehicleActions(vid)
  local veh = getObjectByID(vid)
  if not veh then return {} end
  local model = veh:getJBeamFilename()
  local vd = core_vehicle_manager.getVehicleData(vid)
  local all = {}
  for name, a in pairs(vd and vd.vdata and vd.vdata.inputActions or {}) do all[name] = a end
  for _, path in ipairs(FS:findFiles('/vehicles/' .. model .. '/', 'input_actions*.json', 0, false, false)) do
    for name, a in pairs(jsonReadFile(path) or {}) do all[name] = a end
  end
  local enabled = vd and vd.vdata and vd.vdata.actionsEnabled
  local allowed
  if enabled then
    allowed = {}
    for _, e in pairs(enabled) do allowed[e.name] = true end
  end
  local out = {}
  for name, a in pairs(all) do
    if a.namespace ~= 'common' and (a.ctx == nil or a.ctx == 'vlua') and (not allowed or allowed[name])
       and (a.onDown or a.onChange) then
      out[name] = a
    end
  end
  return out
end

function B.vehicleControls()
  local id = player()
  if not id then return "[]" end
  local list = {}
  for name, a in pairs(vehicleActions(id)) do
    list[#list + 1] = {
      name = name,
      title = translateLanguage(a.title or name, a.title or name),
      desc = a.desc and translateLanguage(a.desc, a.desc) or nil,
      axis = a.onChange ~= nil and a.onDown == nil,
    }
  end
  if #list == 0 then return "[]" end
  table.sort(list, function(x, y) return x.name < y.name end)
  return jsonEncode(list)
end

-- Run one of those controls in the vehicle VM. phase: "down", "up" or "change" (axis, with value).
function B.useControl(name, phase, value)
  local id, veh = player()
  if not veh then error("no player vehicle") end
  local a = vehicleActions(id)[name]
  if not a then error("this car has no control named " .. tostring(name)) end
  local code = (phase == "down" and a.onDown) or (phase == "up" and a.onUp) or (phase == "change" and a.onChange)
  if not code then return "none" end
  -- The input system normally provides these to action code.
  local prelude = string.format("local VALUE, FILTERTYPE, ANGLE, LOCKTYPE, OSCLOCKHP, PLAYER = %s, 2, 0, 0, 0, 0 ",
    tostring(tonumber(value) or (phase == "down" and 1 or 0)))
  be:queueObjectLua(id, prelude .. code)
  return "ok"
end

-- Run a Lua string in the player vehicle's VM.
function B.vlua(code)
  local id, veh = player()
  if not veh then error("no player vehicle") end
  be:queueObjectLua(id, code)
  return "ok"
end

function B.recover()
  local _, veh = player()
  if not veh then error("no player vehicle") end
  B.selfResetAt = os.clock() -- her own recover_car: its result already tells her
  spawn.teleportToLastRoad(veh, {resetVehicle = true})
  return "ok"
end

function B.changeVehicle(model, config)
  core_vehicles.replaceVehicle(model, config and {config = config} or {})
  return "ok"
end

-- Drivable models from the game's own catalog (so mods show up too).
local DRIVABLE = {Car = true, Truck = true, ['Heavy Machinery'] = true}
function B.carModels()
  local out = {}
  for _, m in pairs(core_vehicles.getModelList(true).models or {}) do
    if DRIVABLE[m.Type] and m.key then
      local name = ((m.Brand or '') .. ' ' .. (m.Name or m.key)):gsub('^%s+', '')
      out[#out + 1] = {key = m.key, name = name, type = m.Type}
    end
  end
  table.sort(out, function(a, b) return a.key < b.key end)
  return jsonEncode(out)
end

-- Places to drive to on this map: the game's own big-map points of interest (named areas, gas and
-- charging stations, drag strips, drift spots, missions), plus the waypoint the streamer set on the
-- big map, if any.
function B.destinations()
  if not gameplay_rawPois then extensions.load('gameplay_rawPois') end
  local function tr(s)
    if not s then return nil end
    local ok, r = pcall(translateLanguage, s, s, true)
    return ok and r or s
  end
  local out = {}
  local ok, list = pcall(gameplay_rawPois.getRawPoiListByLevel, getCurrentLevelIdentifier())
  for _, e in ipairs(ok and list or {}) do
    local d, mi = e.data or {}, e.markerInfo or {}
    local pos
    for _, mt in ipairs({'bigmapMarker', 'missionMarker', 'gasStationMarker', 'driftLineMarker', 'vehicleTrigger'}) do
      if mi[mt] and mi[mt].pos then pos = mi[mt].pos break end
    end
    local name, kind = mi.bigmapMarker and mi.bigmapMarker.name, d.type
    if d.missionId and gameplay_missions_missions then
      local m = gameplay_missions_missions.getMissionById(d.missionId)
      if m then name, kind = m.name, 'mission:' .. tostring(m.missionType) end
    end
    if not name and d.facility then name = d.facility.name end
    name = tr(name)
    if pos and kind and name and name ~= '' then
      out[#out + 1] = {name = name, kind = kind, pos = {pos.x, pos.y, pos.z}}
    end
  end
  local waypoint
  local ownRoute = B.pilot and B.pilot.route and not B.pilot.route.userWaypoint -- then the GPS shows hers
  if core_groundMarkers and core_groundMarkers.getTargetPos and not ownRoute then
    local wp = core_groundMarkers.getTargetPos()
    if type(wp) == 'string' then wp = (map.getMap().nodes[wp] or {}).pos end -- a road node name
    if wp and wp.x then waypoint = {wp.x, wp.y, wp.z} end
  end
  return jsonEncode({level = getCurrentLevelIdentifier(), places = out, waypoint = waypoint})
end

function B.carConfigs(model)
  local m = core_vehicles.getModel(model)
  if not m or not m.configs then return "[]" end
  local out = {}
  for key, c in pairs(m.configs) do
    out[#out + 1] = {key = key, name = c.Configuration or c.Name or key, type = c['Config Type'],
                     power = c.Power, desc = c.Description}
  end
  if #out == 0 then return "[]" end
  table.sort(out, function(a, b) return a.key < b.key end)
  return jsonEncode(out)
end

function B.spawnTraffic(amount)
  if not gameplay_traffic then extensions.load('gameplay_traffic') end
  gameplay_traffic.setupTraffic(amount, {keepCurrent = true})
  return "ok"
end

function B.setTimeOfDay(t)
  local tod = core_environment.getTimeOfDay() or {}
  tod.time = t
  core_environment.setTimeOfDay(tod)
  return "ok"
end

function B.setPause(paused)
  simTimeAuthority.pause(paused)
  return "ok"
end

function B.setGravity(g)
  core_environment.setGravity(g)
  return "ok"
end

-- ============================================================================================
-- Pilot: the fast control layer. Neuro decides WHAT to do (follow the road at a speed, which way
-- at the next junction, head for a target); this runs every frame and decides HOW: pure-pursuit
-- steering along a path, and speed control capped for bends, obstacles and arrival.
-- ============================================================================================
B.pilot = B.pilot or {mode = 'off'}
local P = B.pilot
local WHEELBASE, MAX_STEER = 2.7, 0.55 -- metres, radians at full lock (typical car)
local LAT_ACCEL, DECEL = 5.0, 4.5     -- m/s^2: comfortable cornering grip, planned braking

-- Her driving style (the driving_style action), applied on top of those defaults: corner = how much of
-- the grip to use in bends, brake = how hard to plan braking, throttle cap/gain, gap = metres to stop
-- short of obstacles, lane = keep_right | middle | racing_line (cuts corners).
local STYLE = {corner = 1.0, brake = 1.0, throttleCap = 1.0, throttleGain = 0.12, gap = 7, lane = 'own_lane',
               swerve = false, ram = false} -- swerve around things in the way / ram: never brake for them
local function style()
  return P.style or STYLE
end

local function flatDir(v)
  local d = vec3(v.x, v.y, 0)
  if d:length() < 1e-3 then return vec3(0, 1, 0) end
  return d:normalized()
end

local function signedAngle(f, d) -- radians from heading f to direction d, positive = right
  return math.atan2(d.x * f.y - d.y * f.x, d.x * f.x + d.y * f.y)
end

-- Points along the road from the car, taking the chosen branch at the first junction. With
-- `away` (a direction to flee in), every junction takes the branch heading most that way.
-- `side` moves the path out of the normal right-hand lane to get around something: 'left' is the
-- other lane, 'right' the shoulder just past the road edge. The normal lane comes back as `lane`.
-- Which side of the road traffic drives on, from the map's own road rules ("rightHandDrive" maps,
-- like a UK-style one, drive on the left): +1 = keep right, -1 = keep left. Lane offsets below are
-- written for driving on the right and mirrored by this.
local function driveSide()
  local ok, rules = pcall(map.getRoadRules)
  return (ok and rules and rules.rightHandDrive) and -1 or 1
end

local function laneOffset(radius, side)
  local r = radius or 4
  local keep = math.min(2.0, r * 0.45) -- the centre of her own lane, from the road's centre line
  -- Where she drives normally (her driving_style lane), as an offset from the road's centre line,
  -- written for traffic on the right and mirrored below for maps that drive on the left.
  local base = ({own_lane = keep, keep_right = keep, middle = 0, racing_line = 0, other_lane = -keep,
                 shoulder = r + 1.0})[style().lane] or keep
  -- Overtaking: at least P.passGap to the side of where she normally drives (both cars' half-widths
  -- plus room between them), even where the road data says the road is narrower than that.
  local gap = P.passGap or 2.7
  if side == 'auto' and P.passOffset then return P.passOffset end -- the clear line the pilot picked (absolute)
  local sgn = driveSide()
  if side == 'left' then -- 'left' = the other lane: across the road from where she normally drives
    return sgn * (base >= 0 and math.min(-keep, base - gap) or math.max(keep, base + gap))
  end
  if side == 'right' then return sgn * math.max(r + 1.5, base + gap) end -- 'right' = the shoulder
  return sgn * base
end

local function roadPath(o, maxDist, turn, away, side)
  local nodes = map.getMap().nodes
  local adj = adjacency(nodes)
  local n1, n2 = map.findClosestRoad(o.pos)
  if not n1 or not nodes[n1] or not nodes[n2] then return nil end
  local f = flatDir(o.dirVec)
  local cur, prev = n2, n1
  if (nodes[n2].pos - nodes[n1].pos):dot(f) < 0 then cur, prev = n1, n2 end
  local heading = flatDir(nodes[cur].pos - nodes[prev].pos)
  local pts, lane, total, last, junction = {}, {}, 0, o.pos, nil
  for _ = 1, 80 do
    local node = nodes[cur]
    total = total + (node.pos - last):length()
    last = node.pos
    local right = vec3(heading.y, -heading.x, 0)
    lane[#lane + 1] = node.pos + right * laneOffset(node.radius) -- keep right
    pts[#pts + 1] = side and node.pos + right * laneOffset(node.radius, side) or lane[#lane]
    if total >= maxDist then break end
    local cands = {}
    for to in pairs(adj[cur] or {}) do
      if to ~= prev and nodes[to] then
        cands[#cands + 1] = {to = to, ang = signedAngle(heading, flatDir(nodes[to].pos - node.pos))}
      end
    end
    if #cands == 0 then break end
    local pick
    if #cands > 1 and not junction then
      junction = {name = cur, pos = node.pos, dist = total, branches = #cands}
      table.sort(cands, function(a, b) return a.ang < b.ang end) -- leftmost first
      if turn == 'left' then pick = cands[1] elseif turn == 'right' then pick = cands[#cands] end
    end
    if not pick and away and #cands > 1 then
      local bestDot
      for _, c in ipairs(cands) do
        local dot = flatDir(nodes[c.to].pos - node.pos):dot(away)
        if not bestDot or dot > bestDot then pick, bestDot = c, dot end
      end
    end
    if not pick then
      for _, c in ipairs(cands) do
        if not pick or math.abs(c.ang) < math.abs(pick.ang) then pick = c end
      end
    end
    heading = flatDir(nodes[pick.to].pos - node.pos)
    prev, cur = cur, pick.to
  end
  return pts, junction, lane
end

local function isRampHit(res)
  local _, shape = hitInfo(res.obj)
  return res.norm.z > 0.3 and (shape:find('ramp') or shape:find('jump') or shape:find('kicker')) ~= nil
end

-- A level ray runs into any slope in front of the car, so a sloped hit is only an obstacle if the ground
-- there actually rises too steeply to drive up. Measured with two vertical rays either side of the hit.
local MAX_GRADE = 0.8 -- metres of rise per metre (about 39 degrees)
local function groundZ(p)
  local res = castRay(vec3(p.x, p.y, p.z + 3), vec3(p.x, p.y, p.z - 6), true, false)
  return res and res.pt.z
end

-- Only the ground itself gets the slope test: trees and rocks can report sloped normals too.
local function isGround(res)
  local cls = hitInfo(res.obj):lower()
  return cls:find('terrain') ~= nil or cls:find('meshroad') ~= nil or cls:find('groundplane') ~= nil
end

local function climbable(pt, f)
  local z1, z2 = groundZ(pt - f * 0.5), groundZ(pt + f * 1.5)
  return z1 ~= nil and z2 ~= nil and (z2 - z1) / 2 <= MAX_GRADE
end

-- Distance to the nearest obstacle straight ahead (static geometry or a vehicle), or nil.
-- Half the car's width, from its bounding box (the shorter of its two horizontal axes).
local function halfWidth(id)
  if P.widthVid ~= id then
    local hw
    if be:getObjectOOBBIsInitialized(id) then
      for i = 0, 2 do
        local a = vec3(be:getObjectOOBBHalfAxisXYZ(id, i))
        if math.abs(a.z) < a:length() * 0.5 and (not hw or a:length() < hw) then hw = a:length() end
      end
    end
    P.widthVid, P.halfWidth = id, math.max(0.6, math.min(1.6, hw or 0.95))
  end
  return P.halfWidth
end

-- Distance to the nearest obstacle ahead (static geometry or a vehicle), or nil. Three lines are
-- checked: the middle of the car looks the full distance, and its two edges look `sideRange` ahead -
-- far enough to stop for anything a corner of the car would clip, short enough that walls and rails
-- beside a bend don't set them off.
local function obstacleAhead(id, o, f, range, ignoreNear, sideRange, heading, path)
  -- Measured from the front of the car (the far end of its bounding box in the direction looked),
  -- not its middle: a long truck's nose is several metres ahead of the point the game reports for it.
  local function frontToward(dir)
    if not be:getObjectOOBBIsInitialized(id) then return o.pos end
    local c, ext = vec3(be:getObjectOOBBCenterXYZ(id)), 0
    for i = 0, 2 do ext = ext + math.abs(vec3(be:getObjectOOBBHalfAxisXYZ(id, i)):dot(dir)) end
    return vec3(c.x, c.y, o.pos.z) + dir * ext
  end
  local boxes = nearbyBoxes(id, o, range)
  local best, what, bestVid, bestPos
  local function check(name, origin, dir, reach, travelled) -- one straight piece of a sweep
    local res = castSolid(origin, origin + dir * reach)
    if res and P.ignorePos and (res.pt - P.ignorePos):length() < 3 then res = nil end -- she's pushing through it
    if res and res.norm.z < 0.85 and not isRampHit(res) and not (isGround(res) and climbable(res.pt, dir)) then
      local d = travelled + (res.pt - origin):length()
      if not best or d < best then best, what, bestVid, bestPos = d, name .. ': ' .. table.concat({hitInfo(res.obj, res.pt)}, ':'), nil, res.pt end
    end
    for _, b in ipairs(boxes) do
      if b.vid ~= P.targetVid and b.vid ~= P.ignoreVid and math.abs(b.c.z - origin.z) < 4 then
        local t = rayBox(vec3(origin.x, origin.y, b.c.z), dir, reach, b.c, b.axes)
        if t and (not best or travelled + t < best) then
          best, what, bestVid, bestPos = travelled + t, name .. ': car ' .. tostring(b.name), b.vid, origin + dir * t
        end
      end
    end
  end
  local up = vec3(0, 0, 0.8)
  -- The middle looks where the car is steering (the pursuit point), the full distance.
  local front = frontToward(f)
  check('middle', front + up, f, range, 0)
  -- The two front corners sweep the corridor along the path the pilot is about to drive: each corner
  -- follows the planned path shifted half the car's width to its side, a short way ahead. (A straight
  -- line, or a curve from the steering right now, would "see" the car being overtaken when the planned
  -- path clears it, or miss what a corner really clips in a turn.)
  heading = heading or f
  local nose = frontToward(heading)
  local half = halfWidth(id) + 0.15
  local reach = math.min(range, sideRange or range)
  for _, corner in ipairs({{'right corner', 1}, {'left corner', -1}}) do
    local pos, travelled = nose + up + vec3(heading.y, -heading.x, 0) * half * corner[2], 0
    for i, p in ipairs(path or {}) do
      if travelled >= reach or (best and travelled >= best) then break end
      if (p - nose):dot(heading) > 0.5 then -- only the path ahead of the nose
        local along = flatDir((path[i + 1] or p) - (path[i - 1] or o.pos))
        local target = p + up + vec3(along.y, -along.x, 0) * half * corner[2]
        local seg = target - pos
        local len = seg:length()
        if len > 0.3 then
          local step = math.min(len, reach - travelled)
          check(corner[1], pos, seg / len, step, travelled)
          pos, travelled = pos + seg / len * step, travelled + step
        end
      end
    end
    if travelled == 0 then check(corner[1], pos, heading, reach, 0) end -- no path ahead: straight on
  end
  P.obstacleWhat, P.obstacleRay, P.obstacleVid = what, what and what:match('^(%a+ ?%a*):'), bestVid
  P.obstaclePos = bestPos and vec3(bestPos) or nil
  if best and ignoreNear and (front + f * best - ignoreNear):length() < 8 then return nil end
  return best
end

-- Is a vehicle sitting in the normal lane from just beside the car to 25 m ahead? (Still passing it.)
local function laneBusy(id, o, f, lane)
  if P.passObstacle and (P.passObstacle - o.pos):dot(f) > -4 then return true end -- not past it yet
  for _, b in ipairs(nearbyBoxes(id, o, 40)) do
    local along = (b.c - o.pos):dot(f)
    if along > -4 and along < 25 then
      for _, p in ipairs(lane) do
        if vec3(b.c.x - p.x, b.c.y - p.y, 0):length() < 2.5 then return true end
      end
    end
  end
  return false
end

-- Start going around whatever is ahead on `side` ('left' = the other lane, 'right' = the shoulder):
-- out far enough for our half-width + the car in front's half-width + 1 m between them.
-- A polyline resampled every `spacing` metres (flat distance), up to `maxLen` along it.
local function resample(pts, spacing, maxLen)
  local out, carry, total = {pts[1]}, 0, 0
  for i = 2, #pts do
    local a, b = pts[i - 1], pts[i]
    local seg = vec3(b.x - a.x, b.y - a.y, 0):length()
    local t = spacing - carry
    while t <= seg do
      out[#out + 1] = a + (b - a) * (t / seg)
      total = total + spacing
      if total >= maxLen then return out end
      t = t + spacing
    end
    carry = seg - (t - spacing)
  end
  return out
end

-- Which line across the road is clear? Like a self-driving car's planner: candidate lines (her lane,
-- the centre, the other lane, further out, both shoulders) are each checked as a car-wide corridor
-- along the road ahead, with the same sweep the pilot uses for obstacles. Of the lines clear for 35 m,
-- the one closest to where the car is now wins; if none is, the one clear the longest. Returns the
-- line's offset from the road's centre (positive = right), or nil without a road.
local function chooseLine(id, o, f)
  local _, _, lane = roadPath(o, 70, P.turn, nil, nil)
  if not lane or #lane < 2 then return nil end
  local n1 = map.findClosestRoad(o.pos)
  local radius = (n1 and map.getMap().nodes[n1] and map.getMap().nodes[n1].radius) or 4
  local keep = laneOffset(radius)
  local function rightAt(i)
    local d = flatDir((lane[i + 1] or lane[i]) - (lane[i - 1] or o.pos))
    return vec3(d.y, -d.x, 0)
  end
  local here = keep + (o.pos - lane[1]):dot(rightAt(1)) -- where the car is across the road now
  local gap = P.passGap or 2.7
  local sgn = driveSide()
  local cands = {keep, 0, -keep, keep - sgn * gap, -sgn * (radius + 1.2), sgn * math.max(radius + 1.5, math.abs(keep) + gap)}
  local saved = {P.obstacleWhat, P.obstacleRay, P.obstacleVid, P.obstaclePos}
  local best, bestScore
  for _, line in ipairs(cands) do
    local shifted = {}
    for i, p in ipairs(lane) do shifted[i] = p + rightAt(i) * (line - keep) end
    local dense = resample(shifted, 2, 70)
    local look = dense[math.min(#dense, 6)] or dense[#dense]
    local clear = obstacleAhead(id, o, flatDir(look - o.pos), 40, nil, 40, f, dense) or 40
    local score = (clear >= 35 and 1000 or clear * 10) - math.abs(line - here) -- clear first, then least change
    if not bestScore or score > bestScore then best, bestScore = line, score end
  end
  P.obstacleWhat, P.obstacleRay, P.obstacleVid, P.obstaclePos = saved[1], saved[2], saved[3], saved[4]
  return best, keep
end

-- Diagnostic: how clear each candidate line is right now (offset from the road centre -> metres clear).
function B.lineCheck()
  local id = be:getPlayerVehicleID(0)
  local o = map.objects[id]
  local f = flatDir(o.dirVec)
  local _, _, lane = roadPath(o, 70, P.turn, nil, nil)
  if not lane or #lane < 2 then return 'no road' end
  local n1 = map.findClosestRoad(o.pos)
  local radius = (n1 and map.getMap().nodes[n1].radius) or 4
  local keep = laneOffset(radius)
  local function rightAt(i)
    local d = flatDir((lane[i + 1] or lane[i]) - (lane[i - 1] or o.pos))
    return vec3(d.y, -d.x, 0)
  end
  local here = keep + (o.pos - lane[1]):dot(rightAt(1))
  local gap = P.passGap or 2.7
  local out = {string.format('car is %.1f m right of the centre line (road %.1f m wide)', here, 2 * radius)}
  local saved = {P.obstacleWhat, P.obstacleRay, P.obstacleVid, P.obstaclePos}
  local sgn = driveSide()
  for _, line in ipairs({keep, 0, -keep, keep - sgn * gap, -sgn * (radius + 1.2), sgn * math.max(radius + 1.5, math.abs(keep) + gap)}) do
    local shifted = {}
    for i, p in ipairs(lane) do shifted[i] = p + rightAt(i) * (line - keep) end
    local dense = resample(shifted, 2, 70)
    local look = dense[math.min(#dense, 6)] or dense[#dense]
    local clear = obstacleAhead(id, o, flatDir(look - o.pos), 40, nil, 40, f, dense)
    out[#out + 1] = string.format('line %+.1f m: %s', line, clear and string.format('blocked at %.0f m by %s', clear, tostring(P.obstacleWhat)) or 'clear 40 m')
  end
  P.obstacleWhat, P.obstacleRay, P.obstacleVid, P.obstaclePos = saved[1], saved[2], saved[3], saved[4]
  return table.concat(out, ' | ')
end

local function startPass(id, o, side, obstaclePos)
  P.pass, P.path, P.passClear = side, nil, nil
  P.passObstacle = obstaclePos and vec3(obstaclePos) or nil -- a thing (not a car) to stay out until past
  P.passFrom = vec3(o.pos) -- a copy: o.pos is updated in place
  local f = flatDir(o.dirVec)
  local blocker, closest = 1.0, nil
  for _, b in ipairs(nearbyBoxes(id, o, 40)) do -- the nearest car ahead in our lane
    local rel = b.c - o.pos
    local ahead, across = rel:dot(f), math.abs(rel:dot(vec3(f.y, -f.x, 0)))
    if ahead > 0 and across < 2.5 and (not closest or ahead < closest) then
      local w -- its half-width: the shorter of its horizontal half-axes
      for _, ax in ipairs(b.axes) do
        if math.abs(ax.z) < ax:length() * 0.5 and (not w or ax:length() < w) then w = ax:length() end
      end
      closest, blocker = ahead, w or 1.0
    end
  end
  P.passGap = halfWidth(id) + math.max(0.8, math.min(1.8, blocker)) + 1.0
  -- passLeft: which side of the car being passed we go by (the other lane is on the left when driving
  -- on the right); the corner on the other side is the one that could clip it.
  P.passOffset, P.passLeft = nil, (side == 'left') == (driveSide() > 0)
  if side == 'auto' then
    local keep
    P.passOffset, keep = chooseLine(id, o, f)
    if P.passOffset then P.passLeft = P.passOffset < keep else P.pass, P.passLeft = 'left', driveSide() > 0 end
  end
end


-- ---- Routes: driving to a destination across the map ---------------------------------------
-- The game's own route planner (the one behind its GPS) gives the roads to take; the pilot follows
-- them through every junction, keeps right, re-plans if the car gets knocked off the route, drives
-- the last bit straight to the place itself, and stops there. The route shows on screen with the
-- game's GPS arrows.
local function planRoute(o)
  local R = P.route
  local nodes = map.getMap().nodes
  R.pts = {}
  -- dirMult: strongly prefer driving roads in their legal direction
  for _, n in ipairs(map.getPointToPointPath(o.pos, R.dest, nil, 1000) or {}) do
    local nd = nodes[n]
    if nd then R.pts[#R.pts + 1] = {pos = nd.pos, radius = nd.radius} end
  end
  R.pts[#R.pts + 1] = {pos = R.dest, radius = 0, final = true}
  R.idx, R.plannedAt = 1, os.clock()
end

local function endRoute()
  local R = P.route
  if R and not R.userWaypoint and core_groundMarkers then pcall(core_groundMarkers.setPath, nil) end
  P.route = nil
end

-- Route points from where the car is now, up to 160 m ahead: (path to drive, normal-lane path).
local function routePath(o, f)
  local R = P.route
  if not R.pts then planRoute(o) end
  local pts = R.pts
  local best, bestD = R.idx, math.huge
  for i = R.idx, math.min(#pts, R.idx + 12) do -- progress: the nearest of the next few route points
    local d = (pts[i].pos - o.pos):length()
    if d < bestD then best, bestD = i, d end
  end
  R.idx = best
  if R.idx < #pts and (pts[R.idx].pos - o.pos):dot(f) < 0 then R.idx = R.idx + 1 end -- already passed it
  if bestD > 30 and os.clock() - R.plannedAt > 3 then -- knocked off the route: plan again from here
    planRoute(o)
    return routePath(o, f)
  end
  local out, lane, total, last = {}, {}, 0, o.pos
  for i = R.idx, #pts do
    local p = pts[i]
    local before = pts[i - 1] and pts[i - 1].pos or o.pos
    local after = pts[i + 1] and pts[i + 1].pos or p.pos
    local heading = flatDir(after - before)
    local right = vec3(heading.y, -heading.x, 0)
    lane[#lane + 1] = p.final and p.pos or p.pos + right * laneOffset(p.radius)
    out[#out + 1] = (P.pass and not p.final) and p.pos + right * laneOffset(p.radius, P.pass) or lane[#lane]
    total = total + (p.pos - last):length()
    last = p.pos
    if total > 160 then break end
  end
  local rem, prev = (pts[R.idx].pos - o.pos):length(), pts[R.idx].pos
  for i = R.idx + 1, #pts do rem, prev = rem + (pts[i].pos - prev):length(), pts[i].pos end
  R.remaining = rem
  return out, lane
end

local function sendControls(id, throttle, brake, steer)
  be:queueObjectLua(id, string.format(
    "input.event('throttle', %.3f, 2) input.event('brake', %.3f, 2) input.event('steering', %.3f, 2) input.event('parkingbrake', 0, 2)",
    throttle, brake, steer))
end

function B.pilotUpdate(dt)
  if P.mode == 'off' or not dt or dt <= 0 then return end
  local id = be:getPlayerVehicleID(0)
  local o = id and id >= 0 and map.objects[id]
  if not o then return end
  if P.vid ~= id then P.mode = 'off'; P.status = 'car changed'; endRoute(); return end
  local f = flatDir(o.dirVec)
  local v = o.vel:dot(o.dirVec)
  local now = os.clock()

  -- Path to follow.
  local aimPts, targetPos
  if P.mode == 'road' then
    local away
    if P.fleeVid then
      local threat = map.objects[P.fleeVid]
      if threat then
        away = flatDir(o.pos - threat.pos)
        P.fleeDist = (threat.pos - o.pos):length()
      else
        P.fleeVid, P.fleeDist, P.event = nil, nil, 'Whatever you were fleeing from is gone.'
      end
    end
    if not P.path or now - (P.pathAt or 0) > 0.4 then
      local lane
      if P.route then -- the route decides every junction
        P.path, lane = routePath(o, f)
        P.junction = nil
      else
        P.path, P.junction, lane = roadPath(o, 160, P.turn, away, P.pass)
      end
      P.pathAt, P.lane = now, lane
      if P.pass and lane then -- going around: back into the lane once nothing is left in it
        if (o.pos - P.passFrom):length() > 400 then
          P.pass, P.path, P.event = nil, nil, 'You gave up overtaking after 400 m and moved back into your lane.'
        elseif laneBusy(id, o, f, lane) then
          P.passClear = nil
        elseif not P.passClear then
          P.passClear = now
        elseif now - P.passClear > 0.8 and (o.pos - P.passFrom):length() > 8 then
          P.pass, P.path, P.event = nil, nil, 'You got past and moved back into your lane.'
        end
      end
      if P.turn and P.junction and (P.junction.pos - o.pos):length() < 8 then P.passing = P.junction.name end
      if P.passing and (not P.junction or P.junction.name ~= P.passing) then P.turn, P.passing = nil, nil end
    end
    if not P.path or #P.path == 0 then P.status = 'no road nearby'; sendControls(id, 0, 1, 0); return end
    -- Road points can be 20-30 m apart, which would make the pilot aim far ahead and drift across
    -- gently. Spread the path out every 2 m so it aims at exactly the look-ahead distance. When
    -- overtaking, the path starts right beside the car in the passing lane, so it swings out at once.
    local start = o.pos
    if P.pass and P.lane and P.lane[1] then
      local shift = P.path[1] - P.lane[1]
      start = o.pos + vec3(shift.x, shift.y, 0)
    end
    local poly = {start}
    for _, p in ipairs(P.path) do poly[#poly + 1] = p end
    aimPts = resample(poly, 2, 200)
    if #aimPts > 1 then table.remove(aimPts, 1) end
  else -- target
    if P.fleeVid then -- off-road escape: keep aiming at a point straight away from the pursuer
      local threat = map.objects[P.fleeVid]
      if not threat then
        P.mode, P.event = 'off', 'Whatever you were fleeing from is gone.'
        sendControls(id, 0, 0, 0)
        return
      end
      P.fleeDist = (threat.pos - o.pos):length()
      P.targetPos = o.pos + flatDir(o.pos - threat.pos) * 120
    elseif P.targetVid then
      local t = map.objects[P.targetVid]
      if not t then P.mode = 'off'; P.event = 'the target is gone'; sendControls(id, 0, 0, 0); return end
      P.targetPos = t.pos
    end
    targetPos = P.targetPos
    aimPts = {targetPos}
  end
  if P.dodge then -- sidestepping an obstacle: that point first, until the car is level with it
    if (P.dodge - o.pos):dot(f) < 1 then
      P.dodge, P.dodgeTries = nil, 0
    else
      local withDodge = {P.dodge}
      for _, p in ipairs(aimPts) do withDodge[#withDodge + 1] = p end
      aimPts = withDodge
    end
  end

  -- Pure pursuit: aim at the path point one look-ahead distance away.
  local look = math.max(P.pass and 4.5 or 6, math.min(30, 4 + 0.6 * math.abs(v)))
  if style().lane == 'racing_line' then look = look * 1.6 end -- aiming further ahead cuts the corners
  local aim = aimPts[#aimPts]
  for _, p in ipairs(aimPts) do
    if (p - o.pos):length() >= look then aim = p; break end
  end
  if P.dodge and aimPts[1] == P.dodge then aim = P.dodge end -- steer for the sidestep however close it is
  local toAim = aim - o.pos
  local alpha = signedAngle(f, flatDir(toAim))
  local ld = math.max(3, toAim:length())

  -- Learn how this car really turns (it differs per car, load and surface). Bicycle model: yaw rate =
  -- speed * tan(front wheel angle) / wheelbase, so the yaw rate tells the wheel angle actually achieved.
  -- Gentle cornering: a shortfall means less steering lock than assumed -> steer harder. Hard
  -- cornering: a shortfall means the front tyres are sliding (understeer) -> less grip than assumed.
  if P.learnVid ~= id then P.learnVid, P.maxSteer, P.grip = id, MAX_STEER, LAT_ACCEL end
  local understeer = false
  if P.prevF and v > 3 and math.abs(P.steer or 0) > 0.15 and math.abs((P.steer or 0) - (P.prevSteer or 0)) < 0.02 then
    local yaw = signedAngle(P.prevF, f) / dt
    local wheel = math.atan(yaw * WHEELBASE / v) / P.steer -- wheel angle per unit of steering input
    local lateral = math.abs(v * yaw)
    if lateral < 2.5 then
      if wheel > 0.2 and wheel < 1.0 then P.maxSteer = P.maxSteer + (wheel - P.maxSteer) * math.min(1, dt * 0.5) end
    elseif wheel < P.maxSteer * 0.65 and math.abs(P.steer) > 0.4 then
      understeer = true
      P.grip = math.max(2.5, math.min(P.grip, lateral * 0.9))
    elseif P.grip < LAT_ACCEL * 1.4 then
      P.grip = P.grip + dt * 0.05 -- held the corner fine: trust the tyres a little more again
    end
  end
  local steerAngle = math.atan(2 * WHEELBASE * math.sin(alpha) / ld)
  local steer = math.max(-1, math.min(1, steerAngle / P.maxSteer))
  P.prevSteer = P.steer or 0
  P.steer = (P.steer or 0) + (steer - (P.steer or 0)) * math.min(1, dt * 8) -- smooth the wheel
  P.prevF = f

  -- Speed: her target, capped for bends, obstacles and arrival. Bends work like a real planner's speed
  -- profile: the path ahead (from the car's current heading on) is resampled every 3 m, each change of
  -- direction gives a corner radius, and the car must be able to brake from here down to the speed
  -- each corner allows with the grip this car has shown.
  local st = style()
  local decel, grip = DECEL * (st.brake or 1), P.grip * (st.corner or 1)
  local vt, reason = P.speed, nil
  local poly = {o.pos - f * 6, o.pos}
  for _, p in ipairs(aimPts) do -- (a point right beside the car would read as a hairpin)
    if (p - o.pos):length() > 4 then poly[#poly + 1] = p end
  end
  local rs = resample(poly, 3, 6 + math.max(45, v * v / (2 * decel) + 30))
  for i = 3, #rs - 2 do
    local turn = math.abs(signedAngle(flatDir(rs[i] - rs[i - 2]), flatDir(rs[i + 2] - rs[i])))
    if turn > 0.03 then
      local radius = 6 / turn
      local s = math.max(0, (i - 3) * 3) -- metres from the car to this corner along the path
      local allowed = math.sqrt(grip * radius + 2 * decel * math.max(0, s - 2))
      if allowed < vt then vt, reason = allowed, string.format('slowing for a bend %.0f m ahead', s) end
    end
  end
  if math.abs(alpha) > 1.4 then vt, reason = math.min(vt, 4), 'turning around' end
  if understeer then vt, reason = math.min(vt, v * 0.8), 'understeering - easing off' end
  if P.route and P.route.remaining then -- pull up at the destination (distance along the route)
    local rem = P.route.remaining
    local allowed = math.sqrt(math.max(0, 2 * decel * (rem - 6)))
    if allowed < vt then vt, reason = allowed, 'arriving at ' .. (P.route.name or 'your destination') end
    if rem < 10 and math.abs(v) < 1.5 then
      P.event = 'You arrived at ' .. (P.route.name or 'your destination') .. ' and stopped.'
      endRoute()
      P.mode = 'off'
      sendControls(id, 0, 1, 0)
      return
    end
  end
  local ignore = (P.mode == 'target' and P.arrive == 'through') and targetPos or nil
  -- Look for obstacles where the car is about to go (towards the pursuit point), not straight off
  -- the nose: in a bend the nose points at the trees beside the road.
  local lookDir = math.abs(alpha) < 1.2 and flatDir(toAim) or f
  local obs = obstacleAhead(id, o, lookDir, math.max(15, v * 3 + 10), ignore, math.max(8, v * 1.5 + 6), f, aimPts)
  P.obstacle = obs
  -- Swerving (her driving style, and always when fleeing): steer around what's in the way instead of
  -- stopping for it - a stopped or much slower car, a police car cutting in, or something lying on the
  -- road. Walls and rails beside a bend don't count: a thing has to be on the road (or a car).
  local swerve = st.swerve or P.fleeVid ~= nil
  local soon = obs and obs < math.max(25, v * v / (2 * decel) + 15)
  local right = vec3(f.y, -f.x, 0)
  if swerve and soon and P.mode == 'road' and not P.pass then
    local onRoad, side = false, 'left'
    if P.obstacleVid then
      local other = map.objects[P.obstacleVid]
      onRoad = P.fleeVid ~= nil or not other or other.vel:dot(f) < math.max(3, v * 0.7) -- stopped, slow, oncoming
      if other and (other.pos - o.pos):dot(right) < -0.5 then side = 'right' end
    elseif P.obstaclePos then
      for _, p in ipairs(P.lane or {}) do
        if vec3(P.obstaclePos.x - p.x, P.obstaclePos.y - p.y, 0):length() < 2.5 then onRoad = true break end
      end
      if (P.obstaclePos - o.pos):dot(right) < -0.5 then side = 'right' end
    end
    if onRoad then startPass(id, o, 'auto', not P.obstacleVid and P.obstaclePos or nil) end
  elseif swerve and soon and P.mode == 'target' and not P.dodge and P.obstaclePos then
    -- Off-road: sidestep to a point beside the obstacle, away from the side it's on, then carry on.
    local away = (P.obstaclePos - o.pos):dot(right) > 0 and -1 or 1
    P.dodge = P.obstaclePos + right * away * (halfWidth(id) + (P.obstacleVid and 2.2 or 1.6)) + f * 2
  end
  -- Stopped in front of something that will never move (a tree, a rock, a wall end), in any style:
  -- waiting is pointless, so sidestep it - away from the side it's on, then the other side if that's
  -- blocked too. After both, she's told she's blocked. (A car may still move off: "stop" waits for it.)
  if obs and not P.obstacleVid and P.obstaclePos and math.abs(v) < 0.5 and (P.blockedFor or 0) > 1.5
      and (P.dodgeTries or 0) < 2 then
    local away = (P.obstaclePos - o.pos):dot(right) > 0 and -1 or 1
    if (P.dodgeTries or 0) == 1 then away = -(P.dodgeSide or away) end
    P.dodge = P.obstaclePos + right * away * (halfWidth(id) + 1.6) + f * 2
    P.dodgeSide, P.dodgeTries, P.blockedFor = away, (P.dodgeTries or 0) + 1, 0
  end
  local nearCorner = P.passLeft and 'right corner' or 'left corner'
  if P.pass and obs and obs < 10 and P.obstacleRay == nearCorner and (P.passGap or 0) < 4.5 then
    P.passGap = (P.passGap or 2.7) + dt * 1.5 -- it's bent or parked at an angle: swing out wider
    if P.passOffset then P.passOffset = P.passOffset + (P.passLeft and -1 or 1) * dt * 1.5 end
    P.path = nil
  end
  -- push_through: she means to hit that one thing. Done once it's behind her (or after 20 s).
  if P.ignoreUntil then
    local thing = P.ignoreVid and map.objects[P.ignoreVid]
    local at = thing and thing.pos or P.ignorePos
    if os.clock() > P.ignoreUntil or not at or (at - o.pos):dot(f) < -4 then
      P.ignoreVid, P.ignorePos, P.ignoreUntil = nil, nil, nil
    end
  end
  -- A car ahead that's moving: follow it at its speed rather than braking as if it were parked.
  P.obstacleSpeed = nil
  if obs and P.obstacleVid then
    local other = map.objects[P.obstacleVid]
    if other then P.obstacleSpeed = math.max(0, other.vel:dot(f)) end
  end
  if obs and not st.ram then
    local allowed = (P.obstacleSpeed or 0) + math.sqrt(math.max(0, 2 * decel * (obs - (st.gap or 7)))) -- normally 7 m short
    if allowed < vt then
      vt, reason = allowed, (P.obstacleSpeed or 0) > 1 and string.format('following a car %.0f m ahead', obs)
        or string.format('obstacle %.0f m ahead', obs)
    end
  end
  if targetPos and not P.fleeVid then -- a flee target is never "reached"
    local dist = (targetPos - o.pos):length()
    P.targetDist = dist
    if P.arrive == 'stop' then
      local allowed = math.sqrt(math.max(0, 2 * decel * (dist - 5)))
      if allowed < vt then vt, reason = allowed, 'arriving' end
      if dist < 6 and math.abs(v) < 1 then
        P.mode, P.event = 'off', 'You arrived at your target and stopped.'
        sendControls(id, 0, 1, 0)
        return
      end
    elseif P.closest and dist > P.closest + 10 then
      P.mode, P.event = 'off', 'You went past/through your target; the pilot let go of the controls.'
      sendControls(id, 0, 0, 0)
      return
    end
    P.closest = math.min(P.closest or dist, dist)
  end

  -- Can the car actually drive forward?
  local e = B.electrics[id] or {}
  local gear = tostring(e.gear or '')
  P.warn = nil
  if e.engineRunning == 0 then P.warn = e.electric and 'the car is switched off' or 'the engine is off'
  elseif gear == 'N' or gear == 'P' then P.warn = 'you are in ' .. (gear == 'N' and 'neutral' or 'park')
  elseif gear == 'R' then P.warn = 'you are in reverse (the pilot only drives forwards)' end

  -- Speed controller.
  local err = vt - v
  local throttle, brake = 0, 0
  if P.warn and gear == 'R' then
    brake = 0.3
  elseif err > 0 then
    P.integral = math.max(0, math.min(0.4, (P.integral or 0) + err * dt * 0.03))
    throttle = math.max(0, math.min(st.throttleCap or 1, (st.throttleGain or 0.12) * err + P.integral))
  else
    P.integral = math.max(0, (P.integral or 0) - dt * 0.2)
    if err < -0.8 or vt < 0.5 then brake = math.max(0, math.min(1, -0.15 * (st.brake or 1) * err)) end
    if vt < 0.5 and math.abs(v) < 0.5 then brake = 1 end
  end
  sendControls(id, throttle, brake, P.steer)

  -- Stuck: pushing but not moving.
  if throttle > 0.3 and math.abs(v) < 0.5 then
    P.stuckFor = (P.stuckFor or 0) + dt
  else
    P.stuckFor = 0
  end
  -- Blocked: stopped because something is right in front, so the driver has to back out or go elsewhere.
  if obs and obs < (st.gap or 7) + 1.5 and math.abs(v) < 0.5 then -- sitting at its stopping gap
    P.blockedFor = (P.blockedFor or 0) + dt
  else
    P.blockedFor = 0
  end
  P.status = reason or (vt >= P.speed - 0.1 and 'cruising' or 'adjusting speed')
  P.vt = vt
end

-- Commands from the bridge (JSON). Fields: mode ('road'|'target'|'off'), speed (km/h), turn
-- ('left'|'right'|'straight'), pos {x,y,z} or vid, arrive ('stop'|'through').
function B.pilotCommand(json)
  local c = jsonDecode(json)
  local id = be:getPlayerVehicleID(0)
  if c.mode then
    if c.mode == 'off' and P.mode ~= 'off' and id and id >= 0 then sendControls(id, 0, 0, 0) end
    P.mode, P.vid, P.path, P.event, P.stuckFor, P.closest, P.integral = c.mode, id, nil, nil, 0, nil, 0
    P.blockedFor, P.prevF, P.dodge, P.dodgeTries = 0, nil, nil, 0
    P.targetPos, P.targetVid, P.fleeVid, P.fleeDist = nil, nil, nil, nil
  end
  if c.route then -- {pos = {x, y, z}, name, waypoint = the streamer's own map waypoint}
    endRoute()
    P.route = {dest = vec3(c.route.pos[1], c.route.pos[2], c.route.pos[3]), name = c.route.name,
               userWaypoint = c.route.waypoint}
    P.turn, P.passing, P.path = nil, nil, nil
    if core_groundMarkers then pcall(core_groundMarkers.setPath, P.route.dest) end -- show it on screen
  elseif c.mode or c.turn or c.flee_vid then -- any other way of driving replaces the route
    endRoute()
  end
  if c.flee_vid then P.fleeVid, P.path, P.turn = tonumber(c.flee_vid), nil, nil end
  if c.speed then P.speed = c.speed / 3.6 end
  if c.turn then P.turn, P.passing, P.path = (c.turn ~= 'straight') and c.turn or nil, nil, nil end
  if c.pos then P.targetPos = vec3(c.pos[1], c.pos[2], c.pos[3]) end
  if c.vid then P.targetVid = tonumber(c.vid) end
  if c.arrive then P.arrive = c.arrive end
  if c.style then P.style = c.style end -- stays until she changes it
  if c.push_through then -- stop braking for whatever is in front right now
    P.ignoreVid = P.obstacleVid
    P.ignorePos = (not P.obstacleVid and P.obstaclePos) and vec3(P.obstaclePos) or nil
    P.ignoreUntil = os.clock() + 20
  end
  if c.pass then -- 'left' | 'right' | 'none': drive around whatever is in the lane
    local o = id and map.objects[id]
    local thing = not P.obstacleVid and P.obstacle and P.obstacle < 30 and P.obstaclePos or nil
    if c.pass ~= 'none' and o then startPass(id, o, c.pass, thing) else P.pass, P.path = nil, nil end
  elseif c.mode then
    P.pass = nil
  end
  P.speed = P.speed or (30 / 3.6)
  return "ok"
end

local function pilotStatus()
  if P.mode == 'off' and not P.event then return nil end
  local out = {mode = P.mode, speed = (P.speed or 0) * 3.6, status = P.status, warn = P.warn,
               turn = P.turn, stuck = (P.stuckFor or 0) > 2.5, event = P.event, obstacle = P.obstacle,
               targetDist = P.targetDist, limit = (P.vt or 0) * 3.6, obstacleWhat = P.obstacle and P.obstacleWhat,
               obstacleVid = P.obstacle and P.obstacleVid, obstacleSpeed = P.obstacle and P.obstacleSpeed and P.obstacleSpeed * 3.6,
               fleeing = P.fleeVid ~= nil, fleeDist = P.fleeDist, passing = P.pass,
               blocked = (P.blockedFor or 0) > 2 and P.obstacle or nil}
  if P.route and P.mode == 'road' then
    out.route = {name = P.route.name, remaining = P.route.remaining}
  end
  if P.junction and P.mode == 'road' then
    out.junction ={dist = (P.junction.pos - (map.objects[P.vid] and map.objects[P.vid].pos or P.junction.pos)):length(),
                    branches = P.junction.branches, name = P.junction.name}
  end
  P.event = nil
  return out
end
B.pilotStatus = pilotStatus

-- ============================================================================================
-- Ground scan: the rough shape of the ground around the car, like the elevation map a self-driving
-- car builds from its lidar point cloud. Along each bearing, a vertical ray every `step` metres gives
-- the height of whatever the car would drive on (terrain, road, ramp, rock, kerb). Each vertical ray
-- starts 3 m above the last height, so bridges and branches overhead are ignored, and a level ray
-- 0.6 m up between samples catches walls the vertical rays would slip past. A bearing ends at a
-- 'wall' (something steep or over 3 m high) or a 'drop' (more than 12 m down).
-- Returns {step, bearings, scans = [{h = heights relative to the ground under the car, stop, at,
-- tall, objs = [[sample index, "class:shape"], ...]}]}.
-- ============================================================================================
function B.groundScan(range, bearings, step)
  local id = player()
  local o = id and map.objects[id]
  if not o then return jsonEncode({error = "no player vehicle"}) end
  local f = flatDir(o.dirVec)
  local r = vec3(f.y, -f.x, 0)
  local under = castRay(o.pos + vec3(0, 0, 2), o.pos - vec3(0, 0, 4), true, false)
  local z0 = under and under.pt.z or o.pos.z
  local scans = {}
  for i = 0, bearings - 1 do
    local a = math.rad(i * 360 / bearings) -- clockwise from the nose
    local dir = f * math.cos(a) + r * math.sin(a)
    local z, prev = z0, vec3(o.pos.x, o.pos.y, z0)
    local scan = {h = {}, objs = {}}
    for d = step, range + 0.01, step do
      local p = o.pos + dir * d
      local eye = vec3(prev.x, prev.y, z + 0.6)
      local side = castSolid(eye, vec3(p.x, p.y, z + 0.6))
      if side and side.norm.z < 0.5 then -- a near-vertical face between the last sample and this one
        scan.stop, scan.at = 'wall', d - step + (side.pt - eye):length()
        local cls, shape = hitInfo(side.obj)
        scan.objs[#scan.objs + 1] = {#scan.h + 1, cls .. ':' .. shape}
        break
      end
      local res = castSolid(vec3(p.x, p.y, z + 3), vec3(p.x, p.y, z - 12))
      if not res then
        local over = castRay(vec3(p.x, p.y, z + 40), vec3(p.x, p.y, z + 3), true, false)
        scan.stop, scan.at = over and 'wall' or 'drop', d
        if over then
          scan.tall = over.pt.z - z
          local cls, shape = hitInfo(over.obj)
          scan.objs[#scan.objs + 1] = {#scan.h + 1, cls .. ':' .. shape}
        end
        break
      end
      scan.h[#scan.h + 1] = math.floor((res.pt.z - z0) * 10 + 0.5) / 10
      local cls, shape = hitInfo(res.obj, res.pt)
      if not cls:lower():find('terrain') then scan.objs[#scan.objs + 1] = {#scan.h, cls .. ':' .. shape} end
      z, prev = res.pt.z, res.pt
    end
    scans[#scans + 1] = scan
  end
  return jsonEncode({step = step, bearings = bearings, scans = scans})
end

-- Run the pilot every frame through a virtual GE extension (no files installed). Loading a level
-- unloads extensions, so this is re-checked on every poll and the hook re-created when it's gone.
-- The player's car was reset by anything other than her recover_car (the streamer pressing R, a
-- reload, a scenario): queue it as an event so she's told. The game fires this hook for every car.
function B.onVehicleReset(vid)
  if vid ~= be:getPlayerVehicleID(0) or os.clock() - (B.selfResetAt or -100) < 3 then return end
  local o, moved = map.objects[vid], nil
  if o and B.lastPos and B.lastPos.vid == vid then moved = (o.pos - B.lastPos.pos):length() end
  B.events[#B.events + 1] = {text = 'reset', category = 'reset', source = 'game', moved = moved}
  while #B.events > 50 do table.remove(B.events, 1) end
end

-- ============================================================================================
-- Impacts: who hit her, or what she hit. Every frame, the vehicles and props touching her car (the
-- game's own contact list) are noted at first contact with both speeds from the frame before, so the
-- report says who was moving into whom. Fixed things (walls, trees) aren't in that list: for those the
-- direction she was travelling at the moment of impact is checked.
-- ============================================================================================
B.contacts = B.contacts or {}
B.velSnap = B.velSnap or {}
function B.trackContacts()
  local id = be:getPlayerVehicleID(0)
  local o = id and id >= 0 and map.objects[id]
  if not o then return end
  local now = os.clock()
  local police = {}
  if gameplay_police and gameplay_police.getPoliceVehicles then
    for pid in pairs(gameplay_police.getPoliceVehicles() or {}) do police[pid] = true end
  end
  for oid in pairs(o.objectCollisions or {}) do
    local other = map.objects[oid]
    local c = B.contacts[oid]
    if other and (not c or now - c.last > 2) then -- a new contact: who was moving into whom, just before
      local mine, theirs = B.velSnap[id] or o.vel, B.velSnap[oid] or other.vel
      local toThem = other.pos - o.pos
      toThem = toThem:length() > 1e-3 and toThem:normalized() or vec3(0, 1, 0)
      local veh = getObjectByID(oid)
      c = {first = now, name = veh and modelName(veh:getJBeamFilename()) or 'something', police = police[oid] or false,
           ourSpeed = mine:length(), theirSpeed = theirs:length(),
           weInto = math.max(0, mine:dot(toThem)), theyInto = math.max(0, -theirs:dot(toThem))}
      B.contacts[oid] = c
    end
    if c then c.last = now end
  end
  -- velocities this frame, for the next frame's first contacts (her car and anything close)
  B.velSnap = {[id] = vec3(o.vel)}
  for oid, other in pairs(map.objects) do
    if oid ~= id and (other.pos - o.pos):length() < 30 then B.velSnap[oid] = vec3(other.vel) end
  end
  if o.vel:length() > 2 then B.moveDir, B.moveSpeed, B.moveAt = o.vel:normalized(), o.vel:length(), now end
end

-- What she hit or what hit her in the last few seconds (called when a crash is noticed).
function B.lastImpact()
  local id = be:getPlayerVehicleID(0)
  local o = id and id >= 0 and map.objects[id]
  if not o then return 'null' end
  local now, hits = os.clock(), {}
  for oid, c in pairs(B.contacts) do
    if now - c.first < 4 then
      hits[#hits + 1] = {name = c.name, police = c.police, ourSpeed = c.ourSpeed, theirSpeed = c.theirSpeed,
                         weInto = c.weInto, theyInto = c.theyInto, ago = now - c.first}
    elseif now - c.last > 30 then
      B.contacts[oid] = nil
    end
  end
  local out = {vehicles = hits}
  if #hits == 0 and B.moveDir and now - (B.moveAt or 0) < 3 then -- something fixed, in the way she was going
    local origin = o.pos + vec3(0, 0, 0.6)
    local res = castSolid(origin, origin + B.moveDir * (bodyExit(id, origin, B.moveDir) + 4))
    if res then
      local cls, shape = hitInfo(res.obj, res.pt)
      out.fixed = {cls = cls, shape = shape, normalZ = res.norm.z, speed = B.moveSpeed}
    end
  end
  return jsonEncode(out)
end

-- Hooks into the game's update loop and events, through a virtual GE extension (no files installed).
-- Bump HOOK_VERSION when the sink gains a hook, so an already-running game picks it up.
local HOOK_VERSION = 3
function B.ensurePilotHook()
  if not newExtensionProxy then return end
  local loaded = B.pilotProxy and extensions.isExtensionLoaded(B.pilotProxy.extName)
  if loaded and B.pilotHookVersion == HOOK_VERSION then return end
  if not loaded then B.pilotProxy = newExtensionProxy(nil, 'neuroPilot') end
  B.pilotSink = {
    onUpdate = function(_, dtReal, dtSim)
      if neuroBridge and neuroBridge.trackContacts then pcall(neuroBridge.trackContacts) end
      if neuroBridge and neuroBridge.pilotUpdate then
        local ok, err = pcall(neuroBridge.pilotUpdate, dtSim or dtReal)
        if not ok then neuroBridge.pilotError = tostring(err); neuroBridge.pilot.mode = 'off' end
      end
    end,
    onVehicleResetted = function(_, vid)
      if neuroBridge and neuroBridge.onVehicleReset then pcall(neuroBridge.onVehicleReset, vid) end
    end,
  }
  B.pilotProxy:submitEventSinks({B.pilotSink}, {})
  B.pilotHookVersion = HOOK_VERSION
end
B.ensurePilotHook()

return "neuroBridge ready"
