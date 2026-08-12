--!nonstrict
-- RBXForge Studio Plugin - Phase 2A (create_part) + Phase 4A (inspect_hierarchy)
-- + Phase 4B (find_instances) + Phase 4C (inspect_instance)
-- Bridges Roblox Studio and the local RBXForge process over a WebSocket.
--
-- This milestone implements connection management, a ping/pong test message,
-- and four Studio operations: create_part, inspect_hierarchy, find_instances,
-- and inspect_instance (request/response).
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

	local part = Instance.new("Part")
	part.Name = name
	part.Position = position
	part.Size = size
	part.Color = partColor

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

-- Tool handler registry: incoming request messages are dispatched through this
-- table rather than hard-coded branches. Each handler is registered by name with
-- registerTool(); handleRequest looks the tool up here.
local toolHandlers = {}

local function registerTool(name, handler)
	if toolHandlers[name] then
		log("duplicate tool handler registration: " .. tostring(name))
		return
	end
	toolHandlers[name] = handler
end

-- Registered tool handlers (dispatch happens in handleRequest).
registerTool("create_part", handleCreatePart)
registerTool("inspect_hierarchy", handleInspectHierarchy)
registerTool("find_instances", handleFindInstances)
registerTool("inspect_instance", handleInspectInstance)

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
