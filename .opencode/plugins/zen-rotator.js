import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ZEN_PROVIDER = "opencode"
const DAY_MS = 24 * 60 * 60 * 1000
const SHORT_MS = 5 * 60 * 1000

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.dirname(HERE)
const KEYS_FILE = path.join(ROOT, "zen-keys.json")
const STATE_FILE = path.join(ROOT, "zen-rotation-state.json")
const DEBUG_FILE = path.join(ROOT, "zen-rotator.log")

const CLIENT_ERROR = /(FreeTierError|within OpenCode)/i
const AUTH_ERROR = /(invalid\s*api[_ ]?key|auth\s*error|unauthorized|\b401\b)/i
const HARD_ERROR =
  /(usage[_ ]?limit|quota|resource[_ -]?exhausted|limit[_ -]?(exceeded|reached)|exceeded|insufficient|funds|credits?|payment|\b402\b)/i
const RATE_ERROR = /(rate[_ -]?limit|too many requests|\b429\b)/i

let lastProvider = null
const sessionKeys = new Map()

function dbg(message) {
  try {
    fs.appendFileSync(DEBUG_FILE, `[${new Date().toISOString()}] ${message}\n`)
  } catch {}
}

function readKeys() {
  try {
    const parsed = JSON.parse(fs.readFileSync(KEYS_FILE, "utf8").replace(/^\uFEFF/, ""))
    const list = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.keys) ? parsed.keys : []
    return list
      .map((entry, i) =>
        typeof entry === "string"
          ? { label: `key${i + 1}`, key: entry.trim() }
          : { label: String(entry?.label ?? `key${i + 1}`), key: String(entry?.key ?? "").trim() },
      )
      .filter((entry) => entry.key.length > 0)
  } catch (err) {
    dbg(`no se pudo leer ${KEYS_FILE}: ${err}`)
    return []
  }
}

function readState() {
  try {
    const parsed = JSON.parse(fs.readFileSync(STATE_FILE, "utf8").replace(/^\uFEFF/, ""))
    return {
      active: Number.isInteger(parsed?.active) ? parsed.active : 0,
      cooldowns:
        parsed?.cooldowns && typeof parsed.cooldowns === "object" && !Array.isArray(parsed.cooldowns)
          ? { ...parsed.cooldowns }
          : {},
    }
  } catch {
    return { active: 0, cooldowns: {} }
  }
}

function writeState(state) {
  try {
    fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true })
    const tmp = STATE_FILE + ".tmp"
    fs.writeFileSync(tmp, JSON.stringify({ ...state, updatedAt: new Date().toISOString() }, null, 2))
    fs.renameSync(tmp, STATE_FILE)
  } catch (err) {
    dbg(`no se pudo guardar estado: ${err}`)
  }
}

function pickKey(keys, state) {
  const now = Date.now()
  const index = Math.min(Math.max(state.active, 0), Math.max(keys.length - 1, 0))
  if (!(index in state.cooldowns) || state.cooldowns[index] <= now) return index
  let best = -1
  let bestUntil = Infinity
  for (let i = 0; i < keys.length; i++) {
    const until = state.cooldowns[i] ?? 0
    if (until <= now) return i
    if (until < bestUntil) {
      bestUntil = until
      best = i
    }
  }
  return best >= 0 ? best : index
}

function cooldownFor(text) {
  if (!text) return null
  if (CLIENT_ERROR.test(text)) return null
  if (AUTH_ERROR.test(text)) return DAY_MS
  if (HARD_ERROR.test(text)) return DAY_MS
  if (RATE_ERROR.test(text)) return SHORT_MS
  return null
}

function eventText(event) {
  const status = event?.properties?.status
  if (event?.type === "session.status" && status?.type === "retry") {
    return [status.message, status.action?.message, status.action?.reason].filter(Boolean).join(" ")
  }
  if (event?.type === "session.error") {
    const error = event?.properties?.error
    return [error?.name, error?.data?.message, error?.data?.responseBody, error?.data?.statusCode]
      .filter((part) => part !== undefined && part !== null)
      .join(" ")
  }
  return ""
}

export const ZenKeyRotator = async () => ({
  "chat.headers": async (hookInput, output) => {
    const provider = hookInput?.model?.providerID ?? hookInput?.provider?.info?.id ?? null
    lastProvider = provider
    if (!String(provider).startsWith(ZEN_PROVIDER)) return
    const keys = readKeys()
    if (keys.length === 0) {
      dbg(`sin keys configuradas en ${KEYS_FILE}`)
      return
    }
    const state = readState()
    const index = pickKey(keys, state)
    if (state.active !== index) {
      state.active = index
      writeState(state)
    }
    if (hookInput?.sessionID) {
      if (sessionKeys.size > 500) sessionKeys.clear()
      sessionKeys.set(hookInput.sessionID, index)
    }
    const key = keys[index]
    dbg(`request ${hookInput?.model?.id ?? "?"} usa key#${index} (${key.label})`)
    const headers = output?.headers
    if (!headers || typeof headers !== "object") return
    headers.Authorization = `Bearer ${key.key}`
    headers["x-api-key"] = key.key
    headers["x-goog-api-key"] = key.key
  },
  event: async (hookInput) => {
    const event = hookInput?.event
    const text = eventText(event)
    const ms = cooldownFor(text)
    if (ms === null) return
    if (!String(lastProvider).startsWith(ZEN_PROVIDER)) return
    const keys = readKeys()
    if (keys.length === 0) return
    const state = readState()
    const sessionID = event?.properties?.sessionID
    const index = sessionID && sessionKeys.has(sessionID) ? sessionKeys.get(sessionID) : pickKey(keys, state)
    state.cooldowns[index] = Date.now() + ms
    state.active = pickKey(keys, state)
    writeState(state)
    const libres = keys.filter((_, i) => i !== index && (state.cooldowns[i] ?? 0) <= Date.now()).length
    dbg(
      `key#${index} (${keys[index]?.label ?? "?"}) en enfriamiento ${Math.round(ms / 60000)} min por ` +
        `"${String(text).slice(0, 160)}" -> ahora usa key#${state.active} ` +
        `(${keys[state.active]?.label ?? "ninguna"}), libres=${libres}`,
    )
  },
})

dbg(`plugin zen-rotator cargado desde ${HERE}`)
