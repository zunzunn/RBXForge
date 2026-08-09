--!nonstrict
-- RBXForge Studio Plugin - Phase 2A
-- Bridges Roblox Studio and the local RBXForge process over a WebSocket.
--
-- This milestone implements connection management, a ping/pong test message,
-- and one Studio operation: create_part (request/response).
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
	if params.color ~= "red" then
		return sendResponse(id, false, {
			code = "invalid_params",
			message = "unsupported color: " .. tostring(params.color),
		})
	end

	local part = Instance.new("Part")
	part.Name = name
	part.Position = position
	part.Size = size
	part.Color = Color3.new(1, 0, 0)

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
		color = "red",
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
