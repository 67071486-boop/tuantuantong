import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHmac } from 'node:crypto'
import { once } from 'node:events'
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { setTimeout as pause } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { io as connect } from 'socket.io-client'
import { createGateway } from './gateway.mjs'

const secret = 'realtime-gateway-test-secret'
const origin = 'http://localhost:5181'

function ticket(tenant, user, { signingKey = secret, ttl = 30, claims = {} } = {}) {
  const exp = Math.floor(Date.now() / 1000) + ttl
  const payload = Buffer.from(JSON.stringify({ tenant, user, exp, ...claims })).toString('base64url')
  const signature = createHmac('sha256', signingKey).update(payload).digest('base64url')
  return `${payload}.${signature}`
}

async function fixture(t, options = { secret, origins: [origin] }) {
  const { server, io } = createGateway(options)
  t.after(() => io.close())
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const url = `http://127.0.0.1:${server.address().port}`

  function client(token, requestOrigin = origin) {
    const socket = connect(url, {
      autoConnect: false,
      extraHeaders: { Origin: requestOrigin },
      transports: ['websocket'],
      reconnection: false,
      auth: { token },
    })
    t.after(() => socket.disconnect())
    return socket
  }

  function post(payload, key = secret) {
    return fetch(`${url}/notify`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  }

  function notify(tenant, users, key = secret, incoming) {
    return post({ tenant, users, incoming }, key)
  }

  return { url, client, notify, post }
}

test('delivers data-free notifications to the selected account in the selected studio', { timeout: 5000 }, async (t) => {
  const { url, client, notify } = await fixture(t)
  assert.equal((await fetch(`${url}/socket.io/socket.io.js`)).status, 200)
  const recipient = client(ticket('studio_a', 11))
  const colleague = client(ticket('studio_a', 23))
  const otherStudio = client(ticket('studio_b', 11))
  await Promise.all([recipient, colleague, otherStudio].map(async (socket) => {
    const connected = once(socket, 'connect')
    socket.connect()
    await connected
  }))

  const observed = []
  colleague.on('chat:changed', () => observed.push('colleague'))
  otherStudio.on('chat:changed', () => observed.push('other studio'))
  const nextRecipient = once(recipient, 'chat:changed')
  assert.equal((await notify('studio_a', [11])).status, 204)
  assert.deepEqual(await nextRecipient, [])

  const colleagueMarker = once(colleague, 'chat:changed')
  const studioMarker = once(otherStudio, 'chat:changed')
  assert.equal((await notify('studio_a', [23])).status, 204)
  assert.equal((await notify('studio_b', [11])).status, 204)
  await Promise.all([colleagueMarker, studioMarker])
  await pause(50)
  assert.deepEqual(observed.sort(), ['colleague', 'other studio'])
})

test('rejects forged or expired tickets, disallowed origins, and unauthenticated notify', { timeout: 5000 }, async (t) => {
  const { client, notify } = await fixture(t)
  const clients = [
    client(ticket('studio_a', 11, { signingKey: 'wrong' })),
    client(ticket('studio_a', 11, { ttl: -1 })),
    client(ticket('studio_a', 11), 'http://localhost:5999'),
  ]
  await Promise.all(clients.map(async (socket) => {
    const rejected = once(socket, 'connect_error')
    socket.connect()
    await rejected
    assert.equal(socket.connected, false)
  }))
  assert.equal((await notify('studio_a', [11], 'wrong')).status, 401)
  assert.equal((await notify('studio_a', ['11'])).status, 400)
})

test('disconnects a live browser when its ticket expires', { timeout: 5000 }, async (t) => {
  const { client } = await fixture(t)
  const socket = client(ticket('studio_a', 11, { ttl: 2 }))
  const connected = once(socket, 'connect')
  socket.connect()
  await connected
  const [reason] = await once(socket, 'disconnect')
  assert.equal(reason, 'io server disconnect')
  assert.equal(socket.connected, false)
})

test('only new incoming messages signal the recipient, without message contents', { timeout: 5000 }, async (t) => {
  const { client, notify } = await fixture(t)
  const sender = client(ticket('studio_a', 11))
  const recipient = client(ticket('studio_a', 23))
  const otherStudio = client(ticket('studio_b', 23))
  await Promise.all([sender, recipient, otherStudio].map(async (socket) => {
    const connected = once(socket, 'connect')
    socket.connect()
    await connected
  }))

  const observed = []
  sender.on('chat:message', () => observed.push('sender'))
  otherStudio.on('chat:message', () => observed.push('other studio'))
  recipient.on('chat:message', (data) => observed.push(data))

  const normalChange = once(recipient, 'chat:changed')
  assert.equal((await notify('studio_a', [11, 23])).status, 204)
  await normalChange
  assert.equal((await notify('studio_a', [11, 23], secret, { id: -1 })).status, 400)

  const incoming = once(recipient, 'chat:message')
  assert.equal((await notify('studio_a', [11, 23], secret, {
    id: 75, conversation_id: 19, author: 11, text: 'should stay private',
  })).status, 204)
  await incoming
  await pause(50)
  assert.deepEqual(observed, [{ id: 75, conversationId: 19 }])
})


test('routes a visitor message on its own event to only the intended workspace desk', { timeout: 5000 }, async (t) => {
  const { client, post } = await fixture(t)
  const recipient = client(ticket('studio_a', 23))
  const colleague = client(ticket('studio_a', 39))
  const otherStudio = client(ticket('studio_b', 23))
  await Promise.all([recipient, colleague, otherStudio].map(openSocket))

  const unexpected = []
  recipient.on('chat:message', () => unexpected.push('ordinary message'))
  colleague.on('chat:visitor-message', () => unexpected.push('colleague'))
  otherStudio.on('chat:visitor-message', () => unexpected.push('other studio'))

  const visitor = { id: 75, conversation_id: 19 }
  assert.equal((await post({ tenant: 'studio_a', users: [23], visitor_incoming: { ...visitor, phone: 'private' } })).status, 400)
  const changed = once(recipient, 'chat:changed')
  const incoming = once(recipient, 'chat:visitor-message')
  assert.equal((await post({ tenant: 'studio_a', users: [23], visitor_incoming: visitor })).status, 204)
  await changed
  assert.deepEqual(await incoming, [{ id: 75, conversationId: 19, scope: 'visitor' }])
  await pause(40)
  assert.deepEqual(unexpected, [])
})


function presenceTicket(tenant, user, peer, conversation, options = {}) {
  return ticket(tenant, user, { ...options, claims: { purpose: 'presence', peer, conversation } })
}

async function openSocket(socket) {
  const connected = once(socket, 'connect')
  socket.connect()
  await connected
}

async function watchPresence(socket, key) {
  const state = once(socket, 'chat:presence')
  socket.emit('chat:presence.watch', key)
  return (await state)[0]
}

test('keeps the other party online until their last device in this studio disconnects', { timeout: 8000 }, async (t) => {
  const { client } = await fixture(t)
  const viewer = client(ticket('studio_a', 11))
  const phone = client(ticket('studio_a', 23))
  const desktop = client(ticket('studio_a', 23))
  const otherStudio = client(ticket('studio_b', 23))
  await Promise.all([viewer, otherStudio].map(openSocket))
  assert.deepEqual(await watchPresence(viewer, presenceTicket('studio_a', 11, 23, 71)), {
    conversationId: 71, online: false,
  })

  const becameOnline = once(viewer, 'chat:presence')
  await openSocket(phone)
  assert.deepEqual(await becameOnline, [{ conversationId: 71, online: true }])
  await openSocket(desktop)
  phone.disconnect()
  await pause(50)
  const becameOffline = once(viewer, 'chat:presence')
  desktop.disconnect()
  assert.deepEqual(await becameOffline, [{ conversationId: 71, online: false }])
})

test('presence tickets are bound to the signed viewer, studio, purpose, and conversation', { timeout: 5000 }, async (t) => {
  const { client } = await fixture(t)
  const viewer = client(ticket('studio_a', 11))
  const wrongScope = client(presenceTicket('studio_a', 11, 23, 71))
  const rejectedHandshake = once(wrongScope, 'connect_error')
  wrongScope.connect()
  await rejectedHandshake
  await openSocket(viewer)
  const invalid = [
    ticket('studio_a', 11, { claims: { peer: 23, conversation: 71 } }),
    presenceTicket('studio_a', 39, 23, 72),
    presenceTicket('studio_b', 11, 23, 73),
    presenceTicket('studio_a', 11, 23, 74, { ttl: -5 }),
    presenceTicket('studio_a', 11, 23, 75, { signingKey: 'forged' }),
  ]
  const observed = []
  viewer.on('chat:presence', (state) => observed.push(state))
  for (const key of invalid) viewer.emit('chat:presence.watch', key)
  await watchPresence(viewer, presenceTicket('studio_a', 11, 23, 76))
  assert.deepEqual(observed, [{ conversationId: 76, online: false }])
  viewer.emit('chat:presence.watch', null)
  await pause(50)
  await openSocket(client(ticket('studio_a', 23)))
  await pause(50)
  assert.deepEqual(observed, [{ conversationId: 76, online: false }])
})


test('delivers a validated batch with separate recipients and no cross-studio signal', { timeout: 5000 }, async (t) => {
  const { client, post } = await fixture(t)
  const accounts = [client(ticket('studio_a', 11)), client(ticket('studio_a', 23)),
    client(ticket('studio_a', 39)), client(ticket('studio_b', 23))]
  await Promise.all(accounts.map(openSocket))
  const changed = accounts.map(() => 0)
  const incoming = accounts.map(() => [])
  accounts.forEach((socket, index) => {
    socket.on('chat:changed', () => changed[index]++)
    socket.on('chat:message', (message) => incoming[index].push(message))
  })
  const received = Promise.all([once(accounts[1], 'chat:message'), once(accounts[0], 'chat:message')])
  assert.equal((await post({ tenant: 'studio_a', notifications: [
    { users: [11, 23], incoming: { id: 75, conversation_id: 19, author: 11 } },
    { users: [11, 39], incoming: { id: 76, conversation_id: 20, author: 39 } },
    { users: [23] },
  ] })).status, 204)
  await received
  await pause(40)
  assert.deepEqual(changed, [1, 1, 1, 0])
  assert.deepEqual(incoming, [[{ id: 76, conversationId: 20 }], [{ id: 75, conversationId: 19 }], [], []])
})

test('validates every batch item before emitting anything', { timeout: 5000 }, async (t) => {
  const { client, post, notify } = await fixture(t)
  const recipient = client(ticket('studio_a', 11))
  await openSocket(recipient)
  const changed = []
  const messages = []
  recipient.on('chat:changed', () => changed.push(true))
  recipient.on('chat:message', (message) => messages.push(message))
  assert.equal((await post({ tenant: 'studio_a', notifications: [
    { users: [11], incoming: { id: 75, conversation_id: 19, author: null } },
    { users: [11], incoming: { id: -1, conversation_id: 20, author: null } },
  ] })).status, 400)
  const marker = once(recipient, 'chat:changed')
  assert.equal((await notify('studio_a', [11])).status, 204)
  await marker
  await pause(30)
  assert.deepEqual(changed, [true])
  assert.deepEqual(messages, [])
})

test('rejects malformed batches and bad credentials before any signal', { timeout: 5000 }, async (t) => {
  const { client, post, notify } = await fixture(t)
  const recipient = client(ticket('studio_a', 11))
  await openSocket(recipient)
  const observed = []
  recipient.on('chat:changed', () => observed.push('changed'))
  recipient.on('chat:message', () => observed.push('message'))
  const good = { users: [11], incoming: { id: 75, conversation_id: 19, author: null } }
  const bad = [
    { tenant: 'studio_a', notifications: [] },
    { tenant: 'studio_a', notifications: [good], users: [11] },
    { tenant: 'studio_a', notifications: [good], incoming: good.incoming },
    { tenant: 'studio_a', notifications: [good], extra: true },
    { tenant: 'studio_a', notifications: [good, { users: [11], tenant: 'studio_b' }] },
    { tenant: 'studio_a', notifications: [good, { users: ['11'] }] },
    { tenant: 'studio_a', notifications: [good, { users: [Number.MAX_SAFE_INTEGER + 1] }] },
    { tenant: 'studio_a', notifications: [good, { users: [0] }] },
    { tenant: 'studio_a', notifications: [good, { users: [1.5] }] },
    { tenant: 'studio_a', notifications: [good, { users: [true] }] },
    { tenant: 'studio_a', notifications: [good, { users: [] }] },
    { tenant: 'studio_a', notifications: [good, { users: [11], incoming: { ...good.incoming, text: 'private' } }] },
    { tenant: 'studio_a', notifications: [good, null] },
    { tenant: 'studio_a', notifications: Array(21).fill(good) },
    { tenant: 'studio_a', notifications: [{ users: Array(1001).fill(11) }] },
  ]
  for (const payload of bad) assert.equal((await post(payload)).status, 400, JSON.stringify(payload).slice(0, 150))
  assert.equal((await post({ tenant: 'studio_a', notifications: [good] }, 'wrong')).status, 401)
  const marker = once(recipient, 'chat:changed')
  assert.equal((await notify('studio_a', [11])).status, 204)
  await marker
  await pause(30)
  assert.deepEqual(observed, ['changed'])
})

test('accepts 20 full-size member batches without changing the legacy body limit', { timeout: 5000 }, async (t) => {
  const { url, post } = await fixture(t)
  const users = Array.from({ length: 1000 }, (_, index) => 100_000_000 + index)
  const notifications = Array.from({ length: 20 }, (_, index) => ({ users,
    incoming: { id: index + 1, conversation_id: index + 100, author: users[0] } }))
  assert.equal((await post({ tenant: 'studio_a', notifications })).status, 204)
  async function padded(payload, bytes) {
    const body = JSON.stringify(payload)
    assert(Buffer.byteLength(body) <= bytes)
    return fetch(`${url}/notify`, { method: 'POST', headers: {
      Authorization: `Bearer ${secret}`, 'Content-Type': 'application/json',
    }, body: body + ' '.repeat(bytes - Buffer.byteLength(body)) })
  }
  const batch = { tenant: 'studio_a', notifications: [{ users: [11] }] }
  assert.equal((await padded(batch, 320 * 1024)).status, 204)
  assert.equal((await padded(batch, 320 * 1024 + 1)).status, 413)
  const legacy = { tenant: 'studio_a', users: [11] }
  assert.equal((await padded(legacy, 8192)).status, 204)
  assert.equal((await padded(legacy, 8193)).status, 413)
})

test('enforces the encoded batch item boundary before delivering earlier items', { timeout: 5000 }, async (t) => {
  const { client, post } = await fixture(t)
  const recipient = client(ticket('studio_a', 11))
  await openSocket(recipient)
  let changed = 0
  recipient.on('chat:changed', () => changed++)
  const exact = { users: Array(1000).fill(100_000_000_000_000) }
  const base = Buffer.byteLength(JSON.stringify(exact))
  const largerIds = 16 * 1024 - base
  assert(largerIds > 0 && largerIds < 1000)
  for (let index = 0; index < largerIds; index++) exact.users[index] = 1_000_000_000_000_000
  assert.equal(Buffer.byteLength(JSON.stringify(exact)), 16 * 1024)
  assert.equal((await post({ tenant: 'studio_a', notifications: [exact] })).status, 204)
  exact.users[largerIds] = 1_000_000_000_000_000
  assert.equal(Buffer.byteLength(JSON.stringify(exact)), 16 * 1024 + 1)
  assert.equal((await post({ tenant: 'studio_a', notifications: [{ users: [11] }, exact] })).status, 413)
  const marker = once(recipient, 'chat:changed')
  assert.equal((await post({ tenant: 'studio_a', notifications: [{ users: [11] }] })).status, 204)
  await marker
  await pause(30)
  assert.equal(changed, 1)
})


test('tenant-scoped credentials protect sockets, presence, and internal notifications', { timeout: 5000 }, async (t) => {
  const originA = 'https://studio-a.test'
  const originB = 'https://studio-b.test'
  const keyA = 'synthetic-realtime-credential-studio-a'
  const keyB = 'synthetic-realtime-credential-studio-b'
  const { client, notify } = await fixture(t, { tenants: {
    studio_a: { secret: keyA, origins: [originA] },
    studio_b: { secret: keyB, origins: [originB] },
  } })
  const a = client(ticket('studio_a', 11, { signingKey: keyA }), originA)
  const b = client(ticket('studio_b', 11, { signingKey: keyB }), originB)
  await Promise.all([openSocket(a), openSocket(b)])

  for (const socket of [
    client(ticket('studio_b', 11, { signingKey: keyA }), originB),
    client(ticket('studio_b', 11, { signingKey: keyB }), originA),
    client(ticket('studio_a', 11, { signingKey: keyA }), originB),
  ]) {
    const rejected = once(socket, 'connect_error')
    socket.connect()
    await rejected
    assert.equal(socket.connected, false)
  }

  const presence = []
  b.on('chat:presence', (state) => presence.push(state))
  b.emit('chat:presence.watch', presenceTicket('studio_b', 11, 23, 71, { signingKey: keyA }))
  b.emit('chat:presence.watch', presenceTicket('studio_a', 11, 23, 72, { signingKey: keyA }))
  assert.deepEqual(await watchPresence(b, presenceTicket('studio_b', 11, 23, 73, { signingKey: keyB })), {
    conversationId: 73, online: false,
  })
  assert.deepEqual(presence, [{ conversationId: 73, online: false }])

  const observed = []
  a.on('chat:message', (event) => observed.push(event.id))
  b.on('chat:message', (event) => observed.push(event.id))
  assert.equal((await notify('studio_b', [11], keyA, { id: 51, conversation_id: 81, author: null })).status, 401)
  const markerA = once(a, 'chat:message')
  const markerB = once(b, 'chat:message')
  assert.equal((await notify('studio_a', [11], keyA, { id: 61, conversation_id: 81, author: null })).status, 204)
  assert.equal((await notify('studio_b', [11], keyB, { id: 62, conversation_id: 81, author: null })).status, 204)
  assert.deepEqual((await markerA)[0], { id: 61, conversationId: 81 })
  assert.deepEqual((await markerB)[0], { id: 62, conversationId: 81 })
  assert.deepEqual(observed.sort(), [61, 62])
})

test('the standalone gateway refuses exposed tenant keys and mixed legacy settings', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'chat-realtime-fixture-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const path = join(directory, 'tenants.json')
  const key = 'synthetic-realtime-credential-studio-a'
  const entry = { secret: key, origins: ['https://studio-a.test'] }
  writeFileSync(path, JSON.stringify({ tenants: { studio_a: entry } }), { mode: 0o644 })

  function launch(env) {
    const result = spawnSync(process.execPath, [fileURLToPath(new URL('./gateway.mjs', import.meta.url))], {
      env: { TT_CHAT_REALTIME_TENANTS_FILE: path, ...env },
      encoding: 'utf8',
      timeout: 2000,
    })
    assert.equal(result.status, 1)
    assert.doesNotMatch(result.stderr, new RegExp(key))
    return result.stderr
  }

  assert.match(launch({}), /Invalid private realtime tenant configuration/)
  chmodSync(path, 0o600)
  assert.match(launch({ TT_CHAT_REALTIME_SECRET: 'legacy-test-key' }), /cannot be combined/)
  assert.throws(() => createGateway({ tenants: { studio_a: entry, studio_b: entry } }), /Invalid realtime tenant configuration/)
  assert.throws(() => createGateway({ tenants: {
    studio_a: entry,
    studio_b: { ...entry, secret: 'synthetic-realtime-credential-studio-b' },
  } }), /Invalid realtime tenant Origin/)
})
