const assert = require('node:assert/strict');
const {createServer} = require('node:http');
const {createReadStream, statSync, mkdirSync} = require('node:fs');
const {resolve, extname, sep} = require('node:path');
const {createRequire} = require('node:module');
const {chromium, expect} = createRequire(resolve(__dirname, '../../frontend/package.json'))('@playwright/test');
const frontend = process.argv[2] && resolve(process.argv[2]);
const webk = process.argv[3] && resolve(process.argv[3]);
if(!frontend || !webk) throw Error('Usage: first-order-message.cjs <frontend build> <Web K build>');
const orderNo = '20261120000000000021';
const cid = 1000000031, mid = 1000000033;
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6vSAAAAAASUVORK5CYII=';
const user = {id: 7, username: 'synthetic-worker', display_name: '合成打手', identity_plane: 'browser_fixture',
  roles: ['代练员'], is_tenant_admin: false, internal_chat_enabled: true,
  menu_permissions: ['my_orders', 'order.submit_work'], permissions: [], game_ids: [1]};
let state;
const empty = () => ({id: null, has_conversation: false, title: '合成订单群', order_no: orderNo,
  local_order_no: orderNo, is_member: false, can_join: false, can_send: state.mode !== 'readonly',
  archived: false, can_reopen: false, self_member_id: null, members: []});
function message() {
  const payload = state.payload;
  return {id: mid, seq: state.other ? 2 : 1, conversation_id: cid, sender_id: 31, text: payload.text,
    created_at: '2026-11-20T10:00:00Z', revoked: false, reply_to_id: null,
    ...(payload.image && {image: {mime: payload.image.mime, width: 1, height: 1}})};
}
function otherMessage() {
  return {id: mid - 1, seq: 1, conversation_id: cid, sender_id: 32, text: '另一窗口先发的消息',
    created_at: '2026-11-20T09:59:59Z', revoked: false, reply_to_id: null};
}
function conversation() {
  const member = !state.other || state.joined;
  return {...empty(), id: cid, has_conversation: true, kind: 'order', is_member: member, can_join: !member, can_send: member,
    self_member_id: 31, unread_count: 0, unread_mentions_count: 0, read_seq: 1, peer_read_seq: 0,
    last_message: state.payload ? message() : otherMessage(), pinned: false, marked_unread: false, remark: '',
    can_pin_messages: false, pinned_message_id: null,
    members: [{id: 31, name: '合成打手', studio_name: '合成工作室', order_role: 'worker', left_at: null, has_permission: true},
      {id: 32, name: '合成客服', studio_name: '合成工作室', order_role: 'publisher', left_at: null, has_permission: true}]};
}
const mime = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.wasm': 'application/wasm'};
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if(url.pathname === '/socket.io/socket.io.js') {
    res.setHeader('Content-Type', mime['.js']);
    return res.end('window.io=()=>({on(){return this},emit(){return this},connect(){return this},disconnect(){}})');
  }
  if(url.pathname.startsWith('/api/')) {
    state.calls.push([req.method, url.pathname]);
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const payload = raw ? JSON.parse(raw) : {};
    const send = (data, status = 200, msg = 'ok') => {
      res.writeHead(status, {'Content-Type': 'application/json', 'Set-Cookie': 'csrftoken=synthetic_csrf_cookie; Path=/'});
      res.end(JSON.stringify({code: status === 200 ? 0 : status, msg, data}));
    };
    const path = url.pathname.replace(/\/$/, '');
    if(path === '/api/auth/me') return send({user});
    if(path === '/api/auth/csrf') return send({});
    if(path === `/api/business/my-orders/${orderNo}/detail`) return send({order_no: orderNo, source_order_no: '',
      status: 'IN_PROGRESS', game_name: '合成游戏', server_name: '合成区服', project_text: '合成项目',
      content: '合成订单内容', demand: '', time_requirement: '', game_account: '', game_sub_account: '',
      game_role_name: '', is_urgent: false, remark: '', claimed_at: null, temp_amount: '0.00', group_qr_url: null});
    const order = `/api/internal-chat/orders/${orderNo}`;
    if(path === order) return state.mode === 'maintenance'
      ? send(null, 503, '内部沟通暂时不可用') : send(state.payload || state.other ? conversation() : empty());
    if(path === order + '/messages' && req.method === 'POST') {
      state.writes.push(payload);
      if(state.mode === 'race' && !state.other) { state.other = true; return send(null, 409, '请先加入会话'); }
      if(state.mode === 'race' && !state.joined) return send(null, 409, '请先加入会话');
      if(state.writes.length > 1) assert.equal(payload.client_id, state.writes[0].client_id, 'Unchanged retry must keep its original client_id');
      state.payload = payload;
      if(state.mode === 'ack-lost' && state.writes.length === 1) return send(null, 503, '内部沟通暂时不可用');
      return send({conversation: conversation(), messages: [message()]});
    }
    if(path === order + '/join' && req.method === 'POST') {
      assert(state.payload || state.other, 'Explicit join must never create an empty group');
      state.joins++; state.joined = true; return send(conversation());
    }
    if(path.endsWith('/messages') && req.method === 'GET') return send({messages: [...(state.other ? [otherMessage()] : []), ...(state.payload ? [message()] : [])], has_more: false, has_newer: false});
    if(path.endsWith('/image')) return send({mime: 'image/png', data: png});
    if(path === '/api/internal-chat/contacts') return send({contacts: []});
    if(path === '/api/internal-chat/conversations') return send({conversations: (state.payload || state.other) && (!state.other || state.joined) ? [conversation()] : [], cursor: 1, has_more: false, next_offset: null});
    if(path === '/api/internal-chat/unread') return send({unread_count: 0});
    if(path === '/api/internal-chat/sync') return send({cursor: 1, changes: [], has_more: false, order_visibility_token: state.payload ? 'one-group' : 'no-groups'});
    if(path.endsWith('/read')) return send({read_seq: 1});
    if(path.endsWith('/realtime-ticket')) return send({token: 'synthetic-realtime-ticket', user});
    return send([]);
  }
  let root, relative;
  if(url.pathname.startsWith('/internal-chat-webk/')) { root = webk; relative = url.pathname.slice('/internal-chat-webk/'.length) || 'index.html'; }
  else { root = frontend; relative = url.pathname.slice(1); }
  let file = resolve(root, relative);
  if(!file.startsWith(root + sep) || !statSync(file, {throwIfNoEntry: false})?.isFile()) {
    if(root === webk) return res.writeHead(404).end();
    file = resolve(frontend, url.pathname.startsWith('/m/') ? 'm/index.html' : 'index.html');
  }
  res.setHeader('Content-Type', mime[extname(file)] || 'application/octet-stream');
  createReadStream(file).pipe(res);
});

async function run() {
  const browser = await chromium.launch({channel: process.env.TT_WEBK_BROWSER_CHANNEL || 'chrome'});
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const base = 'http://127.0.0.1:' + server.address().port;
  if(process.env.TT_WEBK_TEST_ARTIFACTS) mkdirSync(process.env.TT_WEBK_TEST_ARTIFACTS, {recursive: true});
  try {
    const modes = ['readonly', 'maintenance', 'text', 'image', 'ack-lost', 'race'];
    const requested = process.argv.slice(4);
    for(const mode of requested.length ? requested : modes) {
      if(!modes.includes(mode)) throw Error('Unknown scenario: ' + mode);
      state = {mode, calls: [], writes: [], payload: null, joins: 0, joined: false, other: false};
      const page = await browser.newPage({viewport: {width: 390, height: 844}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      try {
        await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
        await page.goto(`${base}/m/orders/${orderNo}`);
        const toggle = page.getByTestId('detail-open-internal-chat');
        await expect(toggle).toBeVisible();
        expect(state.calls.some(([, path]) => path === `/api/internal-chat/orders/${orderNo}/`)).toBe(false);
        await toggle.click();
        const start = page.locator('[data-order-chat-start]');
        if(mode === 'maintenance') {
          await expect(page.getByText('内部沟通暂时不可用', {exact: true})).toBeVisible();
          await expect(page.getByTestId('detail-open-submit')).toBeVisible();
        } else {
          await expect(start).toBeVisible();
          if(mode === 'readonly') await expect(start.locator('textarea')).toHaveCount(0);
          else {
            if(mode === 'image') await start.locator('input[type=file]').setInputFiles({name: '合成图片.png', mimeType: 'image/png', buffer: Buffer.from(png, 'base64')});
            else await start.locator('textarea').fill(mode === 'ack-lost' ? '确认丢失消息' : '首次合成文字');
            const send = start.getByRole('button', {name: '发送并加入会话'});
            await send.scrollIntoViewIfNeeded();
            await expect(send).toBeInViewport();
            if(process.env.TT_WEBK_TEST_ARTIFACTS) await start.screenshot({path: resolve(process.env.TT_WEBK_TEST_ARTIFACTS, `first-compose-${mode}.png`)});
            await send.click();
            if(mode === 'ack-lost') {
              await expect(start.getByText('内部沟通暂时不可用', {exact: true})).toBeVisible();
              await send.click();
              await expect.poll(() => state.writes.length).toBe(2);
              expect(state.writes[0]).toEqual(state.writes[1]);
            }
            const chat = page.frameLocator('dialog[aria-label="内部沟通"] iframe');
            const bubble = chat.locator(`.chat.active .bubble[data-mid="${mid}"]`);
            await expect(bubble).toBeInViewport({timeout: 30000});
            if(mode === 'image') await expect.poll(() => bubble.locator('img[src^="blob:"]').first().evaluate(image => image.complete && image.naturalWidth === 1)).toBe(true);
            else await expect(bubble).toContainText(state.payload.text);
          }
        }
        expect(state.joins).toBe(mode === 'race' ? 1 : 0);
        if(mode === 'readonly' || mode === 'maintenance') expect(state.writes.length).toBe(0);
        expect(errors).toEqual([]);
        if(process.env.TT_WEBK_TEST_ARTIFACTS) {
          mkdirSync(process.env.TT_WEBK_TEST_ARTIFACTS, {recursive: true});
          await page.screenshot({path: resolve(process.env.TT_WEBK_TEST_ARTIFACTS, `first-${mode}.png`)});
        }
        console.log('First order message:', mode, 'passed');
      } catch(error) {
        console.error('Synthetic API calls:', state.calls);
        throw error;
      } finally { await page.close(); }
    }
  } finally { await browser.close(); server.close(); }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
