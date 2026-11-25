import { createHmac, timingSafeEqual } from 'node:crypto'
import { closeSync, constants, fstatSync, openSync, readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { isAbsolute } from 'node:path'
import { pathToFileURL } from 'node:url'
import { Server } from 'socket.io'

const localOrigins = [
  'http://localhost:5181', 'http://127.0.0.1:5181',
  'http://localhost:4180', 'http://127.0.0.1:4180',
]
const maxBodyBytes = 8192
const maxBatchItems = 20
const maxBatchItemBytes = 16 * 1024
const maxBatchBodyBytes = maxBatchItems * maxBatchItemBytes
const maxTimeoutMs = 2_147_483_647
const offlineGraceMs = 3000

function validTenant(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 63 && !/[^a-zA-Z0-9_-]/.test(value)
}

function validUser(value) {
  return Number.isSafeInteger(value) && value > 0
}

function sameBytes(actual, expected) {
  if (typeof actual !== 'string') return false
  const actualBytes = Buffer.from(actual)
  const expectedBytes = Buffer.from(expected)
  return actualBytes.length === expectedBytes.length && timingSafeEqual(actualBytes, expectedBytes)
}

function verifyTicket(token, tenantKey, purpose) {
  if (typeof token !== 'string' || token.length > 512) return null
  const [encoded, signature, extra] = token.split('.')
  if (!encoded || !signature || extra !== undefined || /[^a-zA-Z0-9_-]/.test(encoded)) return null
  try {
    const claims = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'))
    const { tenant, user, exp } = claims
    const duration = exp * 1000 - Date.now()
    if (claims.purpose !== purpose || !validTenant(tenant) || !validUser(user) ||
        !Number.isSafeInteger(exp) || duration <= 0 || duration > maxTimeoutMs) {
      return null
    }
    const secret = tenantKey(tenant)
    if (!secret) return null
    const expected = createHmac('sha256', secret).update(encoded).digest('base64url')
    return sameBytes(signature, expected) ? claims : null
  } catch {
    return null
  }
}

function room(tenant, user) {
  return `${tenant}:${user}`
}

function hasFields(value, fields) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).every((field) => fields.includes(field))
}

function validOrigin(value) {
  try {
    const origin = new URL(value)
    return origin.protocol === 'https:' && origin.origin === value
  } catch {
    return false
  }
}

function tenantAccess(tenants) {
  if (!tenants || typeof tenants !== 'object' || Array.isArray(tenants)) {
    throw new Error('Invalid realtime tenant configuration')
  }
  const routes = new Map()
  const keys = new Set()
  const origins = new Set()
  for (const [tenant, entry] of Object.entries(tenants)) {
    if (!validTenant(tenant) || !hasFields(entry, ['secret', 'origins']) ||
        typeof entry.secret !== 'string' || !/^[a-zA-Z0-9_-]{32,128}$/.test(entry.secret) ||
        keys.has(entry.secret) || !Array.isArray(entry.origins) || !entry.origins.length) {
      throw new Error('Invalid realtime tenant configuration')
    }
    keys.add(entry.secret)
    for (const origin of entry.origins) {
      if (!validOrigin(origin) || origins.has(origin)) {
        throw new Error('Invalid realtime tenant Origin')
      }
      origins.add(origin)
    }
    routes.set(tenant, { secret: entry.secret, origins: new Set(entry.origins) })
  }
  if (!routes.size) throw new Error('Realtime tenant configuration is empty')
  return { routes, keys: [...keys], origins: [...origins] }
}

function loadTenants(path) {
  if (!isAbsolute(path)) throw new Error('Realtime tenant configuration requires an absolute path')
  try {
    const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    try {
      const metadata = fstatSync(descriptor)
      if (!metadata.isFile() || (metadata.mode & 0o777) !== 0o600) throw new Error()
      const document = JSON.parse(readFileSync(descriptor, 'utf8'))
      if (!hasFields(document, ['tenants']) || !Object.hasOwn(document, 'tenants')) throw new Error()
      return document.tenants
    } finally {
      closeSync(descriptor)
    }
  } catch {
    throw new Error('Invalid private realtime tenant configuration')
  }
}

function validNotification(value, strict = false) {
  if (!value || !Array.isArray(value.users) || !value.users.length || !value.users.every(validUser)) return false
  if (strict && (!hasFields(value, ['users', 'incoming', 'visitor_incoming']) || value.users.length > 1000)) return false
  const { incoming, visitor_incoming: visitor } = value
  if (visitor !== undefined) {
    return incoming === undefined && validUser(visitor?.id) && validUser(visitor.conversation_id)
      && hasFields(visitor, ['id', 'conversation_id'])
  }
  return incoming === undefined || (validUser(incoming?.id) && validUser(incoming.conversation_id)
    && (incoming.author === null || validUser(incoming.author))
    && (!strict || hasFields(incoming, ['id', 'conversation_id', 'author'])))
}

async function notify(request, response, io, permitsTenant) {
  const chunks = []
  let bytes = 0
  let payload
  try {
    for await (const chunk of request.iterator({ destroyOnReturn: false })) {
      bytes += chunk.length
      if (bytes > maxBatchBodyBytes) {
        request.resume()
        response.writeHead(413).end()
        return
      }
      chunks.push(chunk)
    }
    payload = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    response.writeHead(400).end()
    return
  }
  const { tenant } = payload ?? {}
  if (validTenant(tenant) && !permitsTenant(tenant)) {
    response.writeHead(401).end()
    return
  }
  const batched = payload !== null && typeof payload === 'object' && Object.hasOwn(payload, 'notifications')
  if (!validTenant(tenant) || batched && (!hasFields(payload, ['tenant', 'notifications'])
    || !Array.isArray(payload.notifications) || !payload.notifications.length || payload.notifications.length > maxBatchItems)) {
    response.writeHead(400).end()
    return
  }
  if (!batched && bytes > maxBodyBytes) {
    response.writeHead(413).end()
    return
  }
  const notifications = batched ? payload.notifications : [payload]
  if (!notifications.every((item) => validNotification(item, batched))) {
    response.writeHead(400).end()
    return
  }
  if (batched && notifications.some((item) => Buffer.byteLength(JSON.stringify(item)) > maxBatchItemBytes)) {
    response.writeHead(413).end()
    return
  }
  const users = new Set(notifications.flatMap((item) => item.users))
  io.to([...users].map((user) => room(tenant, user))).emit('chat:changed')
  for (const { users, incoming, visitor_incoming: visitor } of notifications) {
    if (visitor !== undefined) {
      io.to(users.map((user) => room(tenant, user))).emit('chat:visitor-message', {
        id: visitor.id, conversationId: visitor.conversation_id, scope: 'visitor',
      })
    }
    if (incoming === undefined) continue
    const recipients = users.filter((user) => user !== incoming.author)
    if (recipients.length) {
      io.to(recipients.map((user) => room(tenant, user))).emit('chat:message', {
        id: incoming.id, conversationId: incoming.conversation_id,
      })
    }
  }
  response.writeHead(204).end()
}

export function createGateway({ secret, origins, tenants }) {
  const scoped = tenants !== undefined
  if (scoped && (secret !== undefined || origins !== undefined)) {
    throw new Error('Realtime tenant credentials cannot be combined with legacy settings')
  }
  if (!scoped && !secret) throw new Error('TT_CHAT_REALTIME_SECRET is required')
  const access = scoped ? tenantAccess(tenants) : null
  const allowedOrigins = access?.origins ?? origins ?? localOrigins
  const keys = access?.keys ?? [secret]
  function tenantKey(tenant, origin) {
    if (!access) return secret
    const entry = access.routes.get(tenant)
    return entry?.origins.has(origin) ? entry.secret : undefined
  }

  const server = createServer({ requestTimeout: 5000 }, (request, response) => {
    if (request.method !== 'POST' || request.url !== '/notify') {
      response.writeHead(404).end()
      return
    }
    const key = keys.find((candidate) => sameBytes(request.headers.authorization, `Bearer ${candidate}`))
    if (!key) {
      response.writeHead(401).end()
      return
    }
    void notify(request, response, io, (tenant) => !access || access.routes.get(tenant)?.secret === key)
  })
  const io = new Server(server, {
    cors: { origin: allowedOrigins },
    // WebSocket connections also need the origin check at the handshake.
    allowRequest: (request, callback) => callback(null, allowedOrigins.includes(request.headers.origin)),
  })
  const offlineTimers = new Map()
  const rooms = io.sockets.adapter.rooms
  const presenceRoom = (account) => `presence:${account}`
  const online = (account) => Boolean(rooms.get(account)?.size || offlineTimers.has(account))

  function publishPresence(account, isOnline) {
    for (const id of rooms.get(presenceRoom(account)) ?? []) {
      const watcher = io.sockets.sockets.get(id)
      const subscription = watcher?.data.presence
      if (subscription?.account !== account) continue
      watcher.emit('chat:presence', { conversationId: subscription.conversationId, online: isOnline })
    }
  }

  function clearPresenceWatch(socket) {
    const subscription = socket.data.presence
    if (!subscription) return
    clearTimeout(subscription.timer)
    delete socket.data.presence
    socket.leave(presenceRoom(subscription.account))
  }

  function watchPresence(socket, token) {
    clearPresenceWatch(socket)
    if (token === null) return
    const claims = verifyTicket(token, (tenant) => tenantKey(tenant, socket.handshake.headers.origin), 'presence')
    const { tenant, user } = socket.data.identity
    if (!claims || claims.tenant !== tenant || claims.user !== user || claims.peer === user ||
        !validUser(claims.peer) || !validUser(claims.conversation)) return
    const account = room(tenant, claims.peer)
    const conversationId = claims.conversation
    const timer = setTimeout(() => clearPresenceWatch(socket), claims.exp * 1000 - Date.now())
    socket.data.presence = { account, conversationId, timer }
    socket.join(presenceRoom(account))
    socket.emit('chat:presence', { conversationId, online: online(account) })
  }

  io.use((socket, next) => {
    const identity = verifyTicket(socket.handshake.auth.token, (tenant) => tenantKey(tenant, socket.handshake.headers.origin))
    if (!identity) {
      next(new Error('invalid realtime ticket'))
      return
    }
    socket.data.identity = identity
    next()
  })
  io.on('connection', (socket) => {
    const { tenant, user, exp } = socket.data.identity
    const account = room(tenant, user)
    const wasOnline = online(account)
    clearTimeout(offlineTimers.get(account))
    offlineTimers.delete(account)
    socket.join(account)
    if (!wasOnline) publishPresence(account, true)
    socket.on('chat:presence.watch', (token) => watchPresence(socket, token))

    const timer = setTimeout(() => socket.disconnect(true), exp * 1000 - Date.now())
    socket.once('disconnect', () => {
      clearTimeout(timer)
      clearPresenceWatch(socket)
      if (rooms.has(account)) return
      // Ticket renewal reconnects the socket without changing the visible state.
      const offline = setTimeout(() => {
        offlineTimers.delete(account)
        publishPresence(account, false)
      }, offlineGraceMs).unref()
      offlineTimers.set(account, offline)
    })
  })
  return { server, io }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { TT_CHAT_REALTIME_SECRET, TT_CHAT_REALTIME_ORIGINS, TT_CHAT_REALTIME_PORT, TT_CHAT_REALTIME_TENANTS_FILE } = process.env
  const port = Number(TT_CHAT_REALTIME_PORT ?? 4191)
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('TT_CHAT_REALTIME_PORT must be a port number')
  }
  const origins = TT_CHAT_REALTIME_ORIGINS?.split(',').map((origin) => origin.trim()).filter(Boolean)
  const tenants = TT_CHAT_REALTIME_TENANTS_FILE ? loadTenants(TT_CHAT_REALTIME_TENANTS_FILE) : undefined
  const { server } = createGateway({ secret: TT_CHAT_REALTIME_SECRET, origins, tenants })
  server.listen(port, '127.0.0.1', () => console.log(`chat realtime ready on 127.0.0.1:${port}`))
}
