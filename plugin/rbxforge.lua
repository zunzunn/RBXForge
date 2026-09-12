--!nonstrict
-- RBXForge Studio Plugin - Phase 2A (create_part) + Phase 4A (inspect_hierarchy)
-- + Phase 4B (find_instances) + Phase 4C (inspect_instance) + Phase 6A (create_script)
-- + Phase 6B (modify_instance) + Phase 7C (insert_asset)
-- Bridges Roblox Studio and the local RBXForge process over a WebSocket.
--
-- This milestone implements connection management, a ping/pong test message,
-- and seven Studio operations: create_part, create_script, modify_instance,
-- insert_asset, inspect_hierarchy, find_instances, and inspect_instance
-- (request/response).
--
-- To run: copy this file into your Studio Plugins folder (use a real file,
-- NOT a symlink - Studio skips symlinks in the plugins directory) and restart
-- Studio. On macOS: ~/Library/Application Support/Roblox/Plugins; on Windows:
-- %LOCALAPPDATA%\Roblox\Plugins. Or run scripts/install-plugin.sh. A "RBXForge"
-- toolbar appears in the Plugins tab.
--
-- Protocol details: see docs/PROTOCOL.md.

local HttpService = game:GetService("HttpService")

local PLUGIN_NAME = "RBXForge"
local PLUGIN_VERSION = "0.1.0"
local PROTOCOL_VERSION = 1
-- The local RBXForge process listens on this URL by default.
local DEFAULT_URL = "ws://127.0.0.1:7676"

local toolbar = plugin:CreateToolbar(PLUGIN_NAME)
local connectButton = toolbar:CreateButton(
	"connect",
	"Connect to the local RBXForge server",
	"",
	"Connect"
)
local disconnectButton = toolbar:CreateButton(
	"disconnect",
	"Disconnect from the local RBXForge server",
	"",
	"Disconnect"
)
local statusButton = toolbar:CreateButton(
	"status",
	"Show connection status",
	"",
	"Status"
)

for _, button in ipairs({ connectButton, disconnectButton, statusButton }) do
	button.ClickableWhenViewportHidden = true
end
disconnectButton:SetActive(false)

local wsClient = nil
local openedConnection = nil
local messageConnection = nil
local closedConnection = nil
local errorConnection = nil

local function log(message)
	print(string.format("[%s] %s", PLUGIN_NAME, tostring(message)))
end

local function clearClient()
	if messageConnection then messageConnection:Disconnect() end
	if openedConnection then openedConnection:Disconnect() end
	if closedConnection then closedConnection:Disconnect() end
	if errorConnection then errorConnection:Disconnect() end
	openedConnection = nil
	messageConnection = nil
	closedConnection = nil
	errorConnection = nil
	wsClient = nil
end

local function send(message)
	if wsClient and wsClient.ConnectionState == Enum.WebStreamClientState.Open then
		local ok, err = pcall(function()
			wsClient:Send(HttpService:JSONEncode(message))
		end)
		if not ok then
			log("failed to send message: " .. tostring(err))
		end
		return ok
	end
	return false
end

local function disconnect(reason)
	if wsClient then
		if reason then
			send({
				type = "bye",
				id = nil,
				version = PROTOCOL_VERSION,
				timestamp = os.time(),
				payload = { reason = reason },
			})
		end
		pcall(function() wsClient:Close() end)
		clearClient()
	end
	connectButton:SetActive(false)
	disconnectButton:SetActive(false)
	if reason then
		log("disconnected: " .. tostring(reason))
	end
end

local function sendResponse(id, ok, resultOrError)
	local payload
	if ok then
		payload = { ok = true, result = resultOrError }
	else
		payload = { ok = false, error = resultOrError }
	end
	send({
		type = "response",
		id = id,
		version = PROTOCOL_VERSION,
		timestamp = os.time(),
		payload = payload,
	})
end

local function validateVec3(value, what)
	if type(value) ~= "table" then
		return nil, what .. " must be an object with numeric x, y, z"
	end
	local x, y, z = value.x, value.y, value.z
	if type(x) ~= "number" or type(y) ~= "number" or type(z) ~= "number" then
		return nil, what .. " must contain numeric x, y, z"
	end
	return Vector3.new(x, y, z)
end

-- Supported create_part colors (Phase 5A). The CLI schema validates the same
-- set first; the plugin re-validates so it can never create a part with a color
-- it does not know how to render.
local PART_COLORS = {
	red    = Color3.new(1, 0, 0),
	blue   = Color3.new(0, 0, 1),
	green  = Color3.new(0, 1, 0),
	yellow = Color3.new(1, 1, 0),
	white  = Color3.new(1, 1, 1),
	black  = Color3.new(0, 0, 0),
	gray   = Color3.new(0.5, 0.5, 0.5),
}

-- Supported create_part materials (Phase 5C). Keep this list aligned with
-- cli/rbxforge.py CREATE_PART_MATERIALS (the CLI validates first; the plugin
-- revalidates and maps to Enum.Material).
local PART_MATERIALS = {
	Plastic = Enum.Material.Plastic,
	SmoothPlastic = Enum.Material.SmoothPlastic,
	Neon = Enum.Material.Neon,
	Wood = Enum.Material.Wood,
	WoodPlanks = Enum.Material.WoodPlanks,
	Metal = Enum.Material.Metal,
	DiamondPlate = Enum.Material.DiamondPlate,
	Concrete = Enum.Material.Concrete,
	Brick = Enum.Material.Brick,
	Glass = Enum.Material.Glass,
	Granite = Enum.Material.Granite,
	Marble = Enum.Material.Marble,
	Slate = Enum.Material.Slate,
	Sand = Enum.Material.Sand,
	Fabric = Enum.Material.Fabric,
	Grass = Enum.Material.Grass,
	Ice = Enum.Material.Ice,
}

local function handleCreatePart(id, params)
	params = params or {}
	local name = params.name
	if type(name) ~= "string" or name == "" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.name must be a non-empty string",
		})
	end
	local position, positionErr = validateVec3(params.position, "params.position")
	if not position then
		return sendResponse(id, false, { code = "invalid_params", message = positionErr })
	end
	local size, sizeErr = validateVec3(params.size, "params.size")
	if not size then
		return sendResponse(id, false, { code = "invalid_params", message = sizeErr })
	end
	local partColor = PART_COLORS[params.color]
	if not partColor then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "unsupported color: " .. tostring(params.color),
		})
	end

	-- Phase 5B: optional physics flags. Omitted inputs default to true so
	-- existing create_part calls behave exactly as before; explicit values must
	-- be booleans (the CLI validates first; the plugin revalidates).
	local anchored = params.anchored
	if anchored == nil then
		anchored = true
	elseif type(anchored) ~= "boolean" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.anchored must be a boolean, got " .. type(anchored),
		})
	end
	local canCollide = params.can_collide
	if canCollide == nil then
		canCollide = true
	elseif type(canCollide) ~= "boolean" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.can_collide must be a boolean, got " .. type(canCollide),
		})
	end

	-- Phase 5C: optional material. Omitted inputs default to Plastic; any
	-- supplied non-string/unsupported/case-mismatched value is rejected.
	local material = params.material
	if material == nil then
		material = "Plastic"
	end
	local partMaterial = PART_MATERIALS[material]
	if type(material) ~= "string" or not partMaterial then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "unsupported material: " .. tostring(material),
		})
	end

	local part = Instance.new("Part")
	part.Name = name
	part.Position = position
	part.Size = size
	part.Color = partColor
	part.Material = partMaterial
	part.Anchored = anchored
	part.CanCollide = canCollide

	local okParent, parentErr = pcall(function()
		part.Parent = workspace
	end)
	if not okParent then
		return sendResponse(id, false, {
			code = "execution_failed",
			message = "could not parent part to workspace: " .. tostring(parentErr),
		})
	end

	log(string.format(
		"created part %s at (%g, %g, %g) size (%g, %g, %g)",
		name, position.X, position.Y, position.Z, size.X, size.Y, size.Z
	))
	return sendResponse(id, true, {
		name = name,
		position = { x = position.X, y = position.Y, z = position.Z },
		size = { x = size.X, y = size.Y, z = size.Z },
		color = params.color,
		material = material,
		anchored = anchored,
		can_collide = canCollide,
	})
end

-- Builds a bounded snapshot of the Workspace instance tree (Phase 4A).
--
-- Returns (node, count, truncated) where:
--   node      - { name, className, children = { node, ... } } ; children is {}
--               either for a real leaf or (signalled by `truncated`) when the
--               depth limit stopped the descent.
--   count     - total number of instance nodes serialized
--   truncated - true if any instance had children that were omitted because the
--               depth limit was reached
--
-- A `maxDepth` of 1 serializes just `instance` itself (its children, if any,
-- are the truncation).
local function buildTree(instance, maxDepth)
	local out = { name = instance.Name, className = instance.ClassName }
	local children = instance:GetChildren()
	local count = 1
	local truncated = false
	if maxDepth <= 1 then
		out.children = {}
		if #children > 0 then
			truncated = true
		end
	else
		local nodes = {}
		for _, child in ipairs(children) do
			local childNode, childCount, childTrunc = buildTree(child, maxDepth - 1)
			table.insert(nodes, childNode)
			count = count + childCount
			if childTrunc then
				truncated = true
			end
		end
		out.children = nodes
	end
	return out, count, truncated
end

local function handleInspectHierarchy(id, params)
	params = params or {}
	local depth = params.depth
	local maxDepth
	if depth == nil then
		maxDepth = 3
	elseif type(depth) ~= "number" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.depth must be a number",
		})
	else
		maxDepth = math.floor(depth)
	end
	if maxDepth < 1 then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.depth must be at least 1",
		})
	end
	if maxDepth > 50 then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.depth must be at most 50",
		})
	end

	local ok, tree, count, truncated = pcall(buildTree, workspace, maxDepth)
	if not ok then
		log("hierarchy snapshot error: " .. tostring(tree))
		return sendResponse(id, false, {
			code = "execution_failed",
			message = tostring(tree),
		})
	end

	log(string.format(
		"inspected workspace hierarchy (depth=%d): %d instances%s",
		maxDepth,
		count,
		truncated and " (truncated)" or ""
	))
	return sendResponse(id, true, {
		root = "Workspace",
		depth = maxDepth,
		count = count,
		truncated = truncated,
		tree = { tree },
	})
end

-- Builds the full Instance path for `instance`, from Workspace down to the
-- instance itself (e.g. "Workspace/Shop/Door"). Path segments are Names joined
-- with "/".
local function buildPath(instance)
	local parts = {}
	local current = instance
	while current do
		table.insert(parts, 1, current.Name)
		if current == workspace then
			break
		end
		current = current.Parent
	end
	return table.concat(parts, "/")
end

-- Searches the live Workspace hierarchy by name (Phase 4B).
--
-- Returns (matches, total, truncated) where:
--   matches   - up to `limit` tables of { name, className, path }; always a list
--   total     - the total number of matches in the live hierarchy
--   truncated - true if more matches exist than were returned (total > #matches)
--
-- The search is a case-insensitive substring match on instance Name and reads
-- the live hierarchy on every request (no caching/indexing). The returned list
-- is bounded by `limit` (max 100 in the handler), so the response is bounded
-- even when the project has many matches.
local function searchWorkspace(query, limit)
	local lowerQuery = string.lower(query)
	local matches = {}
	local total = 0
	for _, instance in ipairs(workspace:GetDescendants()) do
		if string.find(string.lower(instance.Name), lowerQuery, 1, true) then
			total = total + 1
			if #matches < limit then
				table.insert(matches, {
					name = instance.Name,
					className = instance.ClassName,
					path = buildPath(instance),
				})
			end
		end
	end
	return matches, total, total > #matches
end

local DEFAULT_FIND_MAX_RESULTS = 20
local MAX_FIND_RESULTS = 100

local function handleFindInstances(id, params)
	params = params or {}
	local query = params.query
	if type(query) ~= "string" or query == "" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.query must be a non-empty string",
		})
	end
	local maxResults = params.max_results
	local limit
	if maxResults == nil then
		limit = DEFAULT_FIND_MAX_RESULTS
	elseif type(maxResults) ~= "number" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.max_results must be a number",
		})
	else
		limit = math.floor(maxResults)
	end
	if limit < 1 then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.max_results must be at least 1",
		})
	end
	if limit > MAX_FIND_RESULTS then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.max_results must be at most " .. tostring(MAX_FIND_RESULTS),
		})
	end

	local ok, matches, total, truncated = pcall(searchWorkspace, query, limit)
	if not ok then
		log("find_instances search error: " .. tostring(matches))
		return sendResponse(id, false, {
			code = "execution_failed",
			message = tostring(matches),
		})
	end

	log(string.format(
		"find_instances '%s': %d match(es)%s",
		query,
		total,
		truncated and " (truncated)" or ""
	))
	return sendResponse(id, true, {
		query = query,
		max_results = limit,
		total = total,
		count = #matches,
		truncated = truncated,
		matches = matches,
	})
end

-- Splits a path into segments on "." or "/", preserving empty segments so the
-- handler can reject malformed paths like "Workspace..Part" or "Workspace/Part/".
local function splitPathSegments(path)
	local segments = {}
	local index = 1
	while index <= #path do
		local separator = path:find("[%.%/]", index)
		if separator then
			table.insert(segments, path:sub(index, separator - 1))
			index = separator + 1
		else
			table.insert(segments, path:sub(index))
			break
		end
	end
	return segments
end

-- Resolves a validated, Workspace-rooted list of segments to the live instance
-- by walking from workspace with exact child Name lookups. Returns nil when
-- any segment does not exist.
local function resolveSegments(segments)
	local current = workspace
	for index = 2, #segments do
		local child = current:FindFirstChild(segments[index])
		if not child then
			return nil
		end
		current = child
	end
	return current
end

-- Serializes one property value for the wire. Supported types:
--   string / number / boolean - passed through as-is
--   Vector3                    - { x, y, z }
--   Color3                     - { r, g, b }
--   EnumItem                   - { name, value }
--   UDim2 (GuiObject Position/Size) - { x = { scale, offset }, y = { scale, offset } }
--   BrickColor (SpawnLocation TeamColor) - { name, number }
--   Instance (Model PrimaryPart)      - its full path, e.g. "Workspace/Part"
-- Any other type (e.g. CFrame, Ray) is not supported and serializes to nil
-- (the property is omitted). nil values are also omitted by the JSON encoder.
local function serializeValue(value)
	if type(value) == "string" or type(value) == "number" or type(value) == "boolean" then
		return value
	end
	if typeof(value) == "Vector3" then
		return { x = value.X, y = value.Y, z = value.Z }
	elseif typeof(value) == "Color3" then
		return { r = value.R, g = value.G, b = value.B }
	elseif typeof(value) == "EnumItem" then
		return { name = value.Name, value = value.Value }
	elseif typeof(value) == "UDim2" then
		return {
			x = { scale = value.X.Scale, offset = value.X.Offset },
			y = { scale = value.Y.Scale, offset = value.Y.Offset },
		}
	elseif typeof(value) == "BrickColor" then
		return { name = value.Name, number = value.Number }
	elseif typeof(value) == "Instance" then
		return buildPath(value)
	end
	return nil
end

-- Builds the small explicit allowlist of safe properties for `instance`
-- (Phase 4C). Only these class hierarchies are recognized; everything else
-- returns an empty table (identity/path only, no arbitrary reflection).
local function buildProperties(instance)
	local out = {}
	if instance:IsA("SpawnLocation") then
		out.Position = serializeValue(instance.Position)
		out.Size = serializeValue(instance.Size)
		out.Anchored = serializeValue(instance.Anchored)
		out.CanCollide = serializeValue(instance.CanCollide)
		out.Transparency = serializeValue(instance.Transparency)
		out.Enabled = serializeValue(instance.Enabled)
		out.Duration = serializeValue(instance.Duration)
		out.Neutral = serializeValue(instance.Neutral)
		out.TeamColor = serializeValue(instance.TeamColor)
	elseif instance:IsA("BasePart") then
		out.Position = serializeValue(instance.Position)
		out.Size = serializeValue(instance.Size)
		out.Anchored = serializeValue(instance.Anchored)
		out.CanCollide = serializeValue(instance.CanCollide)
		out.Transparency = serializeValue(instance.Transparency)
	elseif instance:IsA("Model") then
		out.PrimaryPart = serializeValue(instance.PrimaryPart)
	elseif instance:IsA("GuiObject") then
		out.Position = serializeValue(instance.Position)
		out.Size = serializeValue(instance.Size)
		out.Visible = serializeValue(instance.Visible)
	end
	return out
end

local function handleInspectInstance(id, params)
	params = params or {}
	local path = params.path
	if type(path) ~= "string" or path == "" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.path must be a non-empty string",
		})
	end
	local segments = splitPathSegments(path)
	if #segments < 2 then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.path must name an instance inside Workspace "
				.. "(e.g. \"Workspace.SpawnLocation\")",
		})
	end
	if segments[1] ~= "Workspace" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.path must start with the Workspace root "
				.. "(e.g. \"Workspace.SpawnLocation\")",
		})
	end
	for _, segment in ipairs(segments) do
		if segment == "" then
			return sendResponse(id, false, {
				code = "invalid_params",
				message = "params.path contains an empty segment "
					.. "(e.g. \"Workspace..Part\" or a trailing separator)",
			})
		end
	end

	local ok, target, properties = pcall(function()
		local instance = resolveSegments(segments)
		if not instance then
			return nil
		end
		return instance, buildProperties(instance)
	end)
	if not ok then
		log("inspect_instance error: " .. tostring(target))
		return sendResponse(id, false, {
			code = "execution_failed",
			message = tostring(target),
		})
	end
	if not target then
		return sendResponse(id, false, {
			code = "not_found",
			message = "instance not found at path: " .. path,
		})
	end

	log(string.format(
		"inspected instance %s (%s)",
		target.Name,
		target.ClassName
	))
	return sendResponse(id, true, {
		name = target.Name,
		className = target.ClassName,
		path = buildPath(target),
		parent_path = buildPath(target.Parent),
		properties = properties,
	})
end

-- Builds the full Instance path for `instance`, from the top of the DataModel
-- down (excluding the root itself), e.g. "ServerScriptService/MyScript" or
-- "Workspace/Shop/Door". Path segments are Names joined with "/". Unlike
-- buildPath (which stops at Workspace), this covers instances anywhere in the
-- game hierarchy (services, ReplicatedStorage, StarterPlayer, ...).
local function buildGamePath(instance)
	local parts = {}
	local current = instance
	while current and current.Parent do
		table.insert(parts, 1, current.Name)
		current = current.Parent
	end
	return table.concat(parts, "/")
end

-- Resolves a validated, DataModel-rooted list of segments to the live instance
-- by walking from the game root with exact child Name lookups (like
-- resolveSegments, but rooted at game instead of workspace). Returns nil when
-- any segment does not exist.
local function resolveGameSegments(segments)
	local current = game
	for _, segment in ipairs(segments) do
		local child = current:FindFirstChild(segment)
		if not child then
			return nil
		end
		current = child
	end
	return current
end

-- Supported create_script types (Phase 6A) mapped to their Instance class. The
-- CLI schema validates the same set first; the plugin re-validates so it can
-- never create a script of a class it does not know how to make.
local SCRIPT_CLASSES = {
	Script = "Script",
	LocalScript = "LocalScript",
	ModuleScript = "ModuleScript",
}

-- Per-type default containers used when `parent_path` is omitted. These are
-- the conventional homes for each script type in a fresh Roblox place.
local DEFAULT_SCRIPT_PARENTS = {
	Script = game.ServerScriptService,
	LocalScript = game.StarterPlayer.StarterPlayerScripts,
	ModuleScript = game.ReplicatedStorage,
}

local function handleCreateScript(id, params)
	params = params or {}
	local name = params.name
	if type(name) ~= "string" or name == "" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.name must be a non-empty string",
		})
	end

	local scriptType = params.type
	if scriptType == nil then
		scriptType = "Script"
	end
	local scriptClass = SCRIPT_CLASSES[scriptType]
	if type(scriptType) ~= "string" or not scriptClass then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "unsupported script type: " .. tostring(scriptType),
		})
	end

	local source = params.source
	if source == nil then
		source = ""
	end
	if type(source) ~= "string" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.source must be a string",
		})
	end

	local parent
	local parentPath = params.parent_path
	if parentPath ~= nil then
		if type(parentPath) ~= "string" or parentPath == "" then
			return sendResponse(id, false, {
				code = "invalid_params",
				message = "params.parent_path must be a non-empty string",
			})
		end
		local segments = splitPathSegments(parentPath)
		for _, segment in ipairs(segments) do
			if segment == "" then
				return sendResponse(id, false, {
					code = "invalid_params",
					message = "params.parent_path contains an empty segment",
				})
			end
		end
		local ok, resolved = pcall(resolveGameSegments, segments)
		if not ok then
			log("create_script resolve error: " .. tostring(resolved))
			return sendResponse(id, false, {
				code = "execution_failed",
				message = tostring(resolved),
			})
		end
		if not resolved then
			return sendResponse(id, false, {
				code = "not_found",
				message = "parent not found at path: " .. parentPath,
			})
		end
		parent = resolved
	else
		parent = DEFAULT_SCRIPT_PARENTS[scriptType]
	end

	local script = Instance.new(scriptClass)
	script.Name = name
	script.Source = source

	local okParent, parentErr = pcall(function()
		script.Parent = parent
	end)
	if not okParent then
		script:Destroy()
		return sendResponse(id, false, {
			code = "execution_failed",
			message = "could not parent script: " .. tostring(parentErr),
		})
	end

	log(string.format(
		"created %s %s in %s (%d chars of source)",
		scriptType, name, buildGamePath(parent), #source
	))
	return sendResponse(id, true, {
		name = name,
		type = scriptType,
		parent_path = buildGamePath(parent),
		path = buildGamePath(script),
		source_length = #source,
	})
end

-- Supported modify_instance property keys (Phase 6B). The CLI schema validates
-- the same allowlist first (and rejects unknown property names before send);
-- the plugin re-validates each value and applies the changes atomically. BasePart
-- properties apply to any BasePart (including SpawnLocation, a BasePart subclass);
-- SpawnLocation properties apply only to SpawnLocation instances.
local BASEPART_MODIFY_PROPERTIES = {
	position = "Position",
	size = "Size",
	anchored = "Anchored",
	can_collide = "CanCollide",
	transparency = "Transparency",
	color = "Color",
	material = "Material",
}

local SPAWNLOCATION_MODIFY_PROPERTIES = {
	enabled = "Enabled",
	duration = "Duration",
	neutral = "Neutral",
	team_color = "TeamColor",
}

-- Supported modify_instance team_color values (Phase 6B) as Roblox BrickColor
-- names. Kept aligned with the 7 create_part colors (shared palette).
local TEAM_COLORS = {
	["Really red"] = BrickColor.new("Really red"),
	["Bright blue"] = BrickColor.new("Bright blue"),
	["Bright green"] = BrickColor.new("Bright green"),
	["Bright yellow"] = BrickColor.new("Bright yellow"),
	["White"] = BrickColor.new("White"),
	["Black"] = BrickColor.new("Black"),
	["Medium stone grey"] = BrickColor.new("Medium stone grey"),
}

-- Validates one BasePart property value and converts it to its Roblox value.
-- Returns (robloxValue, echoValue, err) where err is nil on success; booleans
-- (including false) are valid values, so only err (nil vs string) signals
-- success/failure. Color/material reuse the create_part allowlists.
local function resolveBasePartModifyValue(key, value)
	if key == "position" or key == "size" then
		local vec, vecErr = validateVec3(value, "params.properties." .. key)
		if not vec then
			return nil, nil, vecErr
		end
		return vec, { x = vec.X, y = vec.Y, z = vec.Z }, nil
	elseif key == "anchored" or key == "can_collide" then
		if type(value) ~= "boolean" then
			return nil, nil, "params.properties." .. key .. " must be a boolean"
		end
		return value, value, nil
	elseif key == "transparency" then
		if type(value) ~= "number" then
			return nil, nil, "params.properties.transparency must be a number"
		end
		if value < 0 or value > 1 then
			return nil, nil, "params.properties.transparency must be in the range [0, 1]"
		end
		return value, value, nil
	elseif key == "color" then
		local color = PART_COLORS[value]
		if type(value) ~= "string" or not color then
			return nil, nil, "unsupported color: " .. tostring(value)
		end
		return color, value, nil
	elseif key == "material" then
		local material = PART_MATERIALS[value]
		if type(value) ~= "string" or not material then
			return nil, nil, "unsupported material: " .. tostring(value)
		end
		return material, value, nil
	end
	return nil, nil, "unsupported property: " .. tostring(key)
end

-- Validates one SpawnLocation-only property value and converts it to its
-- Roblox value. Same return contract as resolveBasePartModifyValue.
local function resolveSpawnLocationModifyValue(key, value)
	if key == "enabled" or key == "neutral" then
		if type(value) ~= "boolean" then
			return nil, nil, "params.properties." .. key .. " must be a boolean"
		end
		return value, value, nil
	elseif key == "duration" then
		if type(value) ~= "number" then
			return nil, nil, "params.properties.duration must be a number"
		end
		if value < 0 then
			return nil, nil, "params.properties.duration must be at least 0"
		end
		return value, value, nil
	elseif key == "team_color" then
		local brickColor = TEAM_COLORS[value]
		if type(value) ~= "string" or not brickColor then
			return nil, nil, "unsupported team color: " .. tostring(value)
		end
		return brickColor, value, nil
	end
	return nil, nil, "unsupported property: " .. tostring(key)
end

local function handleModifyInstance(id, params)
	params = params or {}
	local path = params.path
	if type(path) ~= "string" or path == "" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.path must be a non-empty string",
		})
	end
	local properties = params.properties
	if type(properties) ~= "table" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.properties must be an object",
		})
	end
	if next(properties) == nil then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.properties must contain at least one property",
		})
	end

	local segments = splitPathSegments(path)
	if #segments < 2 then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.path must name an instance inside Workspace "
				.. "(e.g. \"Workspace.SpawnLocation\")",
		})
	end
	if segments[1] ~= "Workspace" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.path must start with the Workspace root "
				.. "(e.g. \"Workspace.SpawnLocation\")",
		})
	end
	for _, segment in ipairs(segments) do
		if segment == "" then
			return sendResponse(id, false, {
				code = "invalid_params",
				message = "params.path contains an empty segment "
					.. "(e.g. \"Workspace..Part\" or a trailing separator)",
			})
		end
	end

	local okResolve, target = pcall(resolveSegments, segments)
	if not okResolve then
		log("modify_instance resolve error: " .. tostring(target))
		return sendResponse(id, false, {
			code = "execution_failed",
			message = tostring(target),
		})
	end
	if not target then
		return sendResponse(id, false, {
			code = "not_found",
			message = "instance not found at path: " .. path,
		})
	end

	local isSpawnLocation = target:IsA("SpawnLocation")
	local isBasePart = target:IsA("BasePart")
	if not isSpawnLocation and not isBasePart then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "instance class '" .. target.ClassName .. "' supports no modifiable "
				.. "properties",
		})
	end

	-- Validate every requested property and build the ordered apply/echo lists.
	-- All validation happens before any write so a single bad property rejects
	-- the whole request with nothing applied.
	local writes = {}
	local changed = {}
	for key, value in pairs(properties) do
		local robloxValue, echoValue, err
		local propertyName = BASEPART_MODIFY_PROPERTIES[key]
		if propertyName then
			robloxValue, echoValue, err = resolveBasePartModifyValue(key, value)
		else
			propertyName = SPAWNLOCATION_MODIFY_PROPERTIES[key]
			if propertyName then
				if not isSpawnLocation then
					return sendResponse(id, false, {
						code = "invalid_params",
						message = "property '" .. key .. "' is only supported for "
							.. "SpawnLocation instances",
					})
				end
				robloxValue, echoValue, err = resolveSpawnLocationModifyValue(key, value)
			else
				return sendResponse(id, false, {
					code = "invalid_params",
					message = "unsupported property: " .. tostring(key),
				})
			end
		end
		if err then
			return sendResponse(id, false, { code = "invalid_params", message = err })
		end
		table.insert(writes, { property = propertyName, value = robloxValue })
		changed[key] = echoValue
	end

	-- Snapshot the originals so a failed apply can be rolled back atomically.
	local originals = {}
	for _, write in ipairs(writes) do
		table.insert(originals, { property = write.property, value = target[write.property] })
	end

	local okApply, applyErr = pcall(function()
		for _, write in ipairs(writes) do
			target[write.property] = write.value
		end
	end)
	if not okApply then
		local okRollback = pcall(function()
			for _, original in ipairs(originals) do
				target[original.property] = original.value
			end
		end)
		if not okRollback then
			log("modify_instance rollback error")
		end
		log("modify_instance apply error: " .. tostring(applyErr))
		return sendResponse(id, false, {
			code = "execution_failed",
			message = "could not apply properties: " .. tostring(applyErr),
		})
	end

	log(string.format(
		"modified %s (%s): %d property/properties",
		target.Name,
		target.ClassName,
		#writes
	))
	return sendResponse(id, true, {
		path = buildPath(target),
		className = target.ClassName,
		changed = changed,
	})
end

-- Tool handler registry: incoming request messages are dispatched through this
-- table rather than hard-coded branches. Each handler is registered by name with
-- registerTool(); handleRequest looks the tool up here.
local toolHandlers = {}

-- --------------------------------------------------------------------------- #
-- Phase 7C: Creator Store asset insertion (insert_asset)
-- --------------------------------------------------------------------------- --

local InsertService = game:GetService("InsertService")

-- Offset applied when placing an asset "near" a referenced instance or the
-- project's SpawnLocation (Phase 7C). Fixed and small so the outcome is
-- deterministic and explainable.
local PLACEMENT_OFFSET = Vector3.new(5, 0, 0)

-- Default fallback position used when the project has no SpawnLocation.
local DEFAULT_INSERT_POSITION = Vector3.new(0, 5, 0)

-- Generates a sibling-unique Name for `instance` inside `parent`: when the
-- plain name already exists a numeric suffix is appended ("Cat", "Cat2",
-- "Cat3", ...). The scan is hard-bounded so it can never loop forever.
local function uniqueSiblingName(parent, baseName)
	local name = baseName
	local counter = 2
	while parent:FindFirstChild(name) and counter <= 1000 do
		name = baseName .. tostring(counter)
		counter = counter + 1
	end
	return name
end

-- Positions a loaded asset at `pos`. Models are pivoted (modern PivotTo with a
-- SetPrimaryPartCFrame fallback); bare BaseParts get an absolute Position.
-- Returns (positioned, actualPos); positioned=false for assets with no
-- transform (e.g. decals/audio/textures), which is reported to the caller
-- instead of silently adjusted.
local function positionLoadedAsset(asset, pos)
	if asset:IsA("BasePart") then
		asset.Position = pos
		return true, pos
	elseif asset:IsA("Model") then
		local okPivot = pcall(function() asset:PivotTo(CFrame.new(pos)) end)
		if okPivot then
			return true, pos
		end
		local primary = asset.PrimaryPart or asset:FindFirstChildWhichIsA("BasePart")
		if primary then
			asset.PrimaryPart = primary
			local okPrimary = pcall(function() asset:SetPrimaryPartCFrame(CFrame.new(pos)) end)
			if okPrimary then
				return true, primary.Position
			end
		end
	end
	return false, nil
end

-- Resolves a DataModel-rooted reference path (e.g. "Workspace.SpawnLocation")
-- to a position to place an asset near. Returns (pos, err).
local function referencePosition(refPath)
	local segments = splitPathSegments(refPath)
	for _, segment in ipairs(segments) do
		if segment == "" then
			return nil, "params.reference_path contains an empty segment"
		end
	end
	local target = resolveGameSegments(segments)
	if not target then
		return nil, "reference instance not found at path: " .. refPath
	end
	if target:IsA("BasePart") then
		return target.Position, nil
	elseif target:IsA("Model") and target.PrimaryPart then
		return target.PrimaryPart.Position, nil
	end
	return nil, "reference instance class '" .. target.ClassName .. "' has no position to place near"
end

local function handleInsertAsset(id, params)
	params = params or {}
	local assetId = params.asset_id
	if type(assetId) ~= "string" or not string.match(assetId, "^%d+$") then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.asset_id must be a non-empty string of digits",
		})
	end
	local numericId = tonumber(assetId)
	if not numericId or numericId < 1 then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.asset_id must be a positive integer",
		})
	end

	local parent
	local parentPath = params.parent_path
	if parentPath ~= nil then
		if type(parentPath) ~= "string" or parentPath == "" then
			return sendResponse(id, false, {
				code = "invalid_params",
				message = "params.parent_path must be a non-empty string",
			})
		end
		local segments = splitPathSegments(parentPath)
		for _, segment in ipairs(segments) do
			if segment == "" then
				return sendResponse(id, false, {
					code = "invalid_params",
					message = "params.parent_path contains an empty segment",
				})
			end
		end
		local okResolve, resolved = pcall(resolveGameSegments, segments)
		if not okResolve then
			log("insert_asset resolve error: " .. tostring(resolved))
			return sendResponse(id, false, {
				code = "execution_failed",
				message = tostring(resolved),
			})
		end
		if not resolved then
			return sendResponse(id, false, {
				code = "not_found",
				message = "parent not found at path: " .. parentPath,
			})
		end
		parent = resolved
	else
		parent = workspace
	end

	-- Mutual exclusion of absolute position and reference instance (the CLI
	-- rejects both up front; the plugin re-validates independently).
	local position = params.position
	local referencePath = params.reference_path
	if position ~= nil and referencePath ~= nil then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "params.position and params.reference_path may not both be provided",
		})
	end

	local placementLabel = "default"
	local placementPos
	if position ~= nil then
		local vec, vecErr = validateVec3(position, "params.position")
		if not vec then
			return sendResponse(id, false, { code = "invalid_params", message = vecErr })
		end
		placementPos = vec
		placementLabel = "explicit"
	elseif referencePath ~= nil then
		local refPos, refErr = referencePosition(referencePath)
		if refPos == nil then
			return sendResponse(id, false, { code = "invalid_params", message = refErr })
		end
		placementPos = refPos + PLACEMENT_OFFSET
		placementLabel = "reference"
	end

	-- Load a single asset from the Roblox Catalog / Creator Store by id. The
	-- CLI only sends ids a prior asset_search / recommend_assets returned, so
	-- invalid/nonexistent ids surface here as clear errors instead of being
	-- invented upstream.
	local okLoad, loaded = pcall(function()
		return InsertService:LoadAsset(numericId)
	end)
	if not okLoad then
		log("insert_asset LoadAsset error: " .. tostring(loaded))
		return sendResponse(id, false, {
			code = "execution_failed",
			message = "could not load asset " .. assetId .. ": " .. tostring(loaded),
		})
	end
	if not loaded then
		return sendResponse(id, false, {
			code = "not_found",
			message = "asset not found or not insertable: " .. assetId,
		})
	end

	-- Ensure a sibling-unique name so an existing instance is never shadowed.
	local finalName = uniqueSiblingName(parent, loaded.Name)
	loaded.Name = finalName

	local okParent, parentErr = pcall(function()
		loaded.Parent = parent
	end)
	if not okParent then
		pcall(function() loaded:Destroy() end)
		log("insert_asset parent error: " .. tostring(parentErr))
		return sendResponse(id, false, {
			code = "execution_failed",
			message = "could not parent asset into the project: " .. tostring(parentErr),
		})
	end

	-- Placement: explicit position / near reference / default (beside the
	-- first SpawnLocation, else a sensible spot above the origin).
	local positioned = false
	local finalPosition
	if placementPos then
		local okPlaced, actualPos = positionLoadedAsset(loaded, placementPos)
		positioned = okPlaced
		finalPosition = actualPos
	else
		local spawn = workspace:FindFirstChildWhichIsA("SpawnLocation", true)
		if spawn then
			placementPos = spawn.Position + PLACEMENT_OFFSET
		else
			placementPos = DEFAULT_INSERT_POSITION
		end
		local okPlaced, actualPos = positionLoadedAsset(loaded, placementPos)
		positioned = okPlaced
		finalPosition = actualPos
	end

	local result = {
		asset_id = tostring(numericId),
		name = finalName,
		class = loaded.ClassName,
		parent_path = buildGamePath(parent),
		path = buildGamePath(loaded),
		positioned = positioned,
		placement = placementLabel,
	}
	if positioned then
		result.position = { x = finalPosition.X, y = finalPosition.Y, z = finalPosition.Z }
	end

	log(string.format(
		"inserted asset %d as %s (%s) into %s (placement: %s%s)",
		numericId,
		finalName,
		loaded.ClassName,
		buildGamePath(parent),
		placementLabel,
		positioned and " " .. tostring(finalPosition) or " - not positionable"
	))
	return sendResponse(id, true, result)
end

-- Registered tool handlers (dispatch happens in handleRequest).

local function registerTool(name, handler)
	if toolHandlers[name] then
		log("duplicate tool handler registration: " .. tostring(name))
		return
	end
	toolHandlers[name] = handler
end

-- Registered tool handlers (dispatch happens in handleRequest).
registerTool("create_part", handleCreatePart)
registerTool("create_script", handleCreateScript)
registerTool("modify_instance", handleModifyInstance)
registerTool("inspect_hierarchy", handleInspectHierarchy)
registerTool("find_instances", handleFindInstances)
registerTool("inspect_instance", handleInspectInstance)
registerTool("insert_asset", handleInsertAsset)

local function handleRequest(id, payload)
	local tool = payload.tool
	local handler = toolHandlers[tool]
	if not handler then
		return sendResponse(id, false, {
			code = "unknown_tool",
			message = "unknown tool: " .. tostring(tool),
		})
	end
	log(string.format("executing tool %s (id=%s)", tostring(tool), tostring(id)))
	local ok, err = pcall(handler, id, payload.params)
	if not ok then
		log("tool handler error: " .. tostring(err))
		sendResponse(id, false, {
			code = "execution_failed",
			message = tostring(err),
		})
	end
end

local function handleMessage(message)
	local ok, decoded = pcall(HttpService.JSONDecode, HttpService, message)
	if not ok then
		log("received a non-JSON message: " .. tostring(message))
		return
	end
	local mtype = decoded.type
	local mid = decoded.id
	if mtype == "ping" then
		send({
			type = "pong",
			id = mid,
			version = PROTOCOL_VERSION,
			timestamp = os.time(),
			payload = { message = "pong" },
		})
		log(string.format("pong sent (id=%s)", tostring(mid)))
	elseif mtype == "welcome" then
		local serverInfo = decoded.payload or {}
		log(string.format(
			"connected to RBXForge server (server v%s, protocol %s)",
			tostring(serverInfo.version or "?"),
			tostring(serverInfo.protocol or "?")
		))
	elseif mtype == "error" then
		local errorInfo = decoded.payload or {}
		log(string.format(
			"server error: [%s] %s",
			tostring(errorInfo.code or "?"),
			tostring(errorInfo.message or "?")
		))
	elseif mtype == "request" then
		handleRequest(mid, decoded.payload or {})
	else
		log("ignored message type: " .. tostring(mtype))
	end
end

local function connect()
	if wsClient then
		log("already connected")
		return
	end
	local ok, err = pcall(function()
		wsClient = HttpService:CreateWebStreamClient(
			Enum.WebStreamClientType.WebSocket,
			{ Url = DEFAULT_URL }
		)
	end)
	if not ok then
		log("failed to create WebSocket client: " .. tostring(err))
		log(
			"WebSockets require a recent Roblox Studio. In Studio, check "
			.. "File > Beta Features for WebSockets support, and allow HTTP "
			.. "requests for this plugin when prompted."
		)
		return
	end

	openedConnection = wsClient.Opened:Connect(function(statusCode)
		log("websocket opened (status " .. tostring(statusCode) .. ")")
		connectButton:SetActive(true)
		disconnectButton:SetActive(true)
		send({
			type = "hello",
			id = nil,
			version = PROTOCOL_VERSION,
			timestamp = os.time(),
			payload = {
				name = PLUGIN_NAME,
				version = PLUGIN_VERSION,
				protocol = PROTOCOL_VERSION,
			},
		})
	end)

	messageConnection = wsClient.MessageReceived:Connect(handleMessage)

	closedConnection = wsClient.Closed:Connect(function()
		log("websocket closed")
		clearClient()
		connectButton:SetActive(false)
		disconnectButton:SetActive(false)
	end)

	errorConnection = wsClient.Error:Connect(function(statusCode, errorMessage)
		log(
			"websocket error (status "
			.. tostring(statusCode)
			.. "): "
			.. tostring(errorMessage)
		)
		clearClient()
		connectButton:SetActive(false)
		disconnectButton:SetActive(false)
	end)

	log("connecting to " .. DEFAULT_URL .. " ...")
end

connectButton.Click:Connect(connect)

disconnectButton.Click:Connect(function()
	disconnect("user requested disconnect")
end)

statusButton.Click:Connect(function()
	if wsClient then
		log("connection state: " .. tostring(wsClient.ConnectionState))
	else
		log("not connected")
	end
end)

log(string.format(
	"%s plugin v%s loaded. Click 'Connect' to connect to the local RBXForge process.",
	PLUGIN_NAME,
	PLUGIN_VERSION
))
