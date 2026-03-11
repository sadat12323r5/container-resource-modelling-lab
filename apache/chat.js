const state = {
    room: localStorage.getItem("chat.room") || "general",
    user: localStorage.getItem("chat.user") || "alice",
    pollMs: Number(localStorage.getItem("chat.pollMs") || 2000),
    lastMessageId: 0,
    knownIds: new Set(),
    pollTimer: null,
};

const roomInput = document.getElementById("roomInput");
const userInput = document.getElementById("userInput");
const pollInput = document.getElementById("pollInput");
const connectButton = document.getElementById("connectButton");
const reloadButton = document.getElementById("reloadButton");
const sendButton = document.getElementById("sendButton");
const composerForm = document.getElementById("composerForm");
const messageInput = document.getElementById("messageInput");
const messagesEl = document.getElementById("messages");
const statusText = document.getElementById("statusText");
const messageCount = document.getElementById("messageCount");
const pollLabel = document.getElementById("pollLabel");
const activeRoomLabel = document.getElementById("activeRoomLabel");
const serviceLabel = document.getElementById("serviceLabel");
const healthBadge = document.getElementById("healthBadge");

function setInputsFromState() {
    roomInput.value = state.room;
    userInput.value = state.user;
    pollInput.value = String(state.pollMs);
    pollLabel.textContent = `Polling every ${state.pollMs / 1000}s`;
    activeRoomLabel.textContent = state.room;
}

function setStatus(text, tone = "neutral") {
    statusText.textContent = text;
    statusText.style.color =
        tone === "error" ? "#8d2c2c" :
        tone === "success" ? "#1f6b52" :
        "#5e6a73";
}

function updateServiceLabel(headers) {
    const targetUs = headers.get("X-Service-Target-Us");
    const actualMs = headers.get("X-Service-Actual-Ms");
    if (!targetUs && !actualMs) {
        serviceLabel.textContent = "n/a";
        return;
    }
    const target = targetUs ? `${targetUs} us` : "?";
    const actual = actualMs ? `${actualMs} ms` : "?";
    serviceLabel.textContent = `${target} / ${actual}`;
}

function renderEmptyState(message) {
    messagesEl.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = message;
    messagesEl.appendChild(empty);
}

function formatTimestamp(tsNs) {
    if (!tsNs) {
        return "unknown time";
    }
    const millis = Math.floor(Number(tsNs) / 1e6);
    if (!Number.isFinite(millis)) {
        return "unknown time";
    }
    return new Date(millis).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    });
}

function appendMessage(message, highlight = false) {
    if (state.knownIds.has(message.id)) {
        return;
    }

    const stickToBottom =
        messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;

    const card = document.createElement("article");
    card.className = `message-card${highlight ? " new-message" : ""}`;

    const meta = document.createElement("div");
    meta.className = "message-meta";

    const user = document.createElement("span");
    user.className = "message-user";
    user.textContent = `${message.user || "anon"} in #${message.room || state.room}`;

    const stamp = document.createElement("span");
    stamp.textContent = `#${message.id} at ${formatTimestamp(message.ts_unix_ns)}`;

    const text = document.createElement("div");
    text.className = "message-text";
    text.textContent = message.text || "";

    meta.appendChild(user);
    meta.appendChild(stamp);
    card.appendChild(meta);
    card.appendChild(text);
    messagesEl.appendChild(card);

    state.knownIds.add(message.id);
    state.lastMessageId = Math.max(state.lastMessageId, Number(message.id) || 0);

    if (stickToBottom) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }
}

function resetRoomState() {
    state.lastMessageId = 0;
    state.knownIds = new Set();
    renderEmptyState(`No messages yet in #${state.room}.`);
}

async function fetchHealth() {
    try {
        const response = await fetch("/health", { cache: "no-store" });
        if (!response.ok) {
            throw new Error(`health ${response.status}`);
        }
        healthBadge.textContent = "Online";
        healthBadge.style.color = "#1f6b52";
    } catch (error) {
        healthBadge.textContent = "Offline";
        healthBadge.style.color = "#8d2c2c";
    }
}

async function fetchMessages({ reset = false } = {}) {
    const room = state.room.trim();
    if (!room) {
        setStatus("Room is required before fetching messages.", "error");
        return;
    }

    if (reset) {
        resetRoomState();
    }

    const sinceId = reset ? 0 : state.lastMessageId;
    const query = new URLSearchParams({
        room,
        since_id: String(sinceId),
        limit: "100",
    });

    try {
        const response = await fetch(`/messages?${query.toString()}`, { cache: "no-store" });
        updateServiceLabel(response.headers);
        if (!response.ok) {
            throw new Error(`messages ${response.status}`);
        }
        const payload = await response.json();
        const items = Array.isArray(payload.messages) ? payload.messages : [];

        if (reset) {
            messagesEl.innerHTML = "";
        }

        if (items.length === 0 && state.knownIds.size === 0) {
            renderEmptyState(`No messages yet in #${state.room}.`);
        } else {
            for (const item of items) {
                appendMessage(item, !reset);
            }
        }

        messageCount.textContent = `${state.knownIds.size} message${state.knownIds.size === 1 ? "" : "s"} loaded`;
        setStatus(`Connected to #${state.room} as ${state.user}.`, "success");
    } catch (error) {
        setStatus(`Failed to fetch messages for #${state.room}.`, "error");
    }
}

async function sendMessage(event) {
    event.preventDefault();
    const text = messageInput.value.trim();
    if (!text) {
        setStatus("Message text is required.", "error");
        return;
    }

    sendButton.disabled = true;

    try {
        const response = await fetch("/send", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                room: state.room,
                user: state.user,
                text,
            }),
        });
        updateServiceLabel(response.headers);
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
            throw new Error(payload.error || `send ${response.status}`);
        }
        if (messagesEl.firstChild && messagesEl.firstChild.classList.contains("empty-state")) {
            messagesEl.innerHTML = "";
        }
        appendMessage(payload.message, true);
        messageCount.textContent = `${state.knownIds.size} message${state.knownIds.size === 1 ? "" : "s"} loaded`;
        messageInput.value = "";
        setStatus(`Message sent to #${state.room}.`, "success");
    } catch (error) {
        setStatus(`Failed to send message: ${error.message}`, "error");
    } finally {
        sendButton.disabled = false;
    }
}

function startPolling() {
    if (state.pollTimer) {
        clearInterval(state.pollTimer);
    }
    state.pollTimer = setInterval(() => {
        fetchMessages();
        fetchHealth();
    }, state.pollMs);
}

function applySessionSettings() {
    state.room = roomInput.value.trim() || "general";
    state.user = userInput.value.trim() || "anon";
    state.pollMs = Number(pollInput.value) || 2000;

    localStorage.setItem("chat.room", state.room);
    localStorage.setItem("chat.user", state.user);
    localStorage.setItem("chat.pollMs", String(state.pollMs));

    setInputsFromState();
    resetRoomState();
    fetchHealth();
    fetchMessages({ reset: true });
    startPolling();
}

connectButton.addEventListener("click", applySessionSettings);
reloadButton.addEventListener("click", () => fetchMessages({ reset: true }));
composerForm.addEventListener("submit", sendMessage);

setInputsFromState();
resetRoomState();
fetchHealth();
fetchMessages({ reset: true });
startPolling();
