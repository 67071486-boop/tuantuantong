const {createReadStream, statSync} = require('node:fs');
const {createServer} = require('node:http');
const {createRequire} = require('node:module');
const {extname, resolve, sep} = require('node:path');

const {chromium, expect} = createRequire(resolve(__dirname, '../../frontend/package.json'))('@playwright/test');
const assetRoot = process.argv[2] && resolve(process.argv[2]);
if(!assetRoot) throw new Error('Usage: node src/webk-preview/tests/reopen-history.cjs <Web K build directory>');

const prefix = '/internal-chat-webk/';
const orderNo = '1'.repeat(20);
const mime = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
  '.woff2': 'font/woff2', '.wasm': 'application/wasm', '.png': 'image/png',
  '.svg': 'image/svg+xml', '.json': 'application/json'
};

function bitmap(width, height) {
  const stride = (width * 3 + 3) & ~3;
  const image = Buffer.alloc(54 + stride * height);
  image.write('BM');
  image.writeUInt32LE(image.length, 2);
  image.writeUInt32LE(54, 10);
  image.writeUInt32LE(40, 14);
  image.writeInt32LE(width, 18);
  image.writeInt32LE(height, 22);
  image.writeUInt16LE(1, 26);
  image.writeUInt16LE(24, 28);
  image.writeUInt32LE(stride * height, 34);
  image.fill(0x42, 54);
  return image;
}

function installHost({count, h5, group, system, historyOrderNo, authorityMode, orderNumber, copy, visitor, conversationId, wideFromVisitor}) {
  const self = {id: 11, display_name: '测试客服'};
  const contact = system ? {id: 2147483648, name: '系统通知', avatar_url: null} :
    {id: 23, name: '测试同事', avatar_url: null};
  const other = {id: 24, name: '另一位测试同事', avatar_url: null};
  const members = [
    {id: 31, name: self.display_name, order_role: 'publisher', left_at: null},
    {id: 32, name: '测试打手', order_role: 'worker', left_at: null, has_permission: false}
  ];
  const messages = Array.from({length: count}, (_, index) => ({
    id: 1001 + index, seq: index + 1, conversation_id: 10,
    sender_id: wideFromVisitor && index === 0 ? self.id : group ? 32 : contact.id,
    text: system ? '命中「我要退款」\n企微会话摘要：群内提出退款，原因待确认。' : '测试消息 ' + (index + 1),
    ...(system && {system_order_no: orderNumber}),
    created_at: '2026-06-15T08:00:00Z', revoked: false, reply_to_id: null
  }));
  if(copy) {
    messages[0].text = '第一行 <正文>\n  第二行 🐱';
    messages[1].noforwards = true;
    messages[2].revoked = true;
  }
  const otherMessage = {
    id: 150, seq: 1, conversation_id: 20, sender_id: other.id, text: '另一段会话的消息',
    created_at: wideFromVisitor ? '2026-06-15T08:05:00Z' : '2026-06-15T08:00:00Z', revoked: false, reply_to_id: null
  };
  let read = h5 || system ? count - 1 : count;
  let authorityStatus = authorityMode === 'authority-cold-link' ? 503 : 200;
  let holdList = false, heldList, forceSync = false, partialList = false, reorderedPages = false;
  let archived = authorityMode === 'order-history-archived-member';
  const canReopen = authorityMode !== 'archive-worker';
  let incoming;
  const calls = [];
  const attachedImages = new Map();
  const orderPath = `/api/internal-chat/orders/${orderNumber}/`;
  const send = (data) => document.querySelector('iframe').contentWindow.postMessage(data, location.origin);
  const respond = (id, result) => send({type: 'tt:chat.response', id, result});
  const history = (rows, params) => {
    const around = Number(params.get('around'));
    if(around) {
      const index = rows.findIndex(({id}) => id === around);
      if(index === -1) return;
      return {
        messages: rows.slice(Math.max(0, index - 30), index + 31),
        has_more: index > 30, has_newer: index + 31 < rows.length
      };
    }
    const before = Number(params.get('before'));
    const older = before ? rows.filter(({seq}) => seq < before) : rows;
    return {
      messages: older.slice(-60), has_more: older.length > 60,
      has_newer: Boolean(older.length && older.at(-1).seq < rows.length)
    };
  };
  const conversation = (id, peer, lastMessage, readSeq, unread = 0) => ({
    id, last_message: lastMessage ?? null, read_seq: readSeq, unread_count: unread, peer_read_seq: 0,
    first_unread_message_id: unread ? lastMessage.id : null, marked_unread: false,
    ...(group && id === 10 ? {
      kind: 'order', title: '测试订单群', members, self_member_id: authorityMode === 'order-history-join' ? null : 31,
      is_member: authorityMode !== 'order-history-join', can_join: authorityMode === 'order-history-join',
      order_no: orderNumber, local_order_no: orderNumber, can_pin_messages: false, pinned_message_id: null,
      archived, can_reopen: canReopen, can_send: !archived
    } : {kind: system && id === 10 ? 'system' : 'direct', peer})
  });

  window.ttOpenChatWide = (source, targetId) => {
    if(source !== document.querySelector('iframe').contentWindow) return false;
    fixture.wide = {conversationId: targetId, fromClick: navigator.userActivation.isActive};
    const params = new URLSearchParams({wide: '1', count: String(count)});
    if(targetId) params.set('conversationId', String(targetId));
    if(wideFromVisitor) {
      params.set('visitor', visitor);
      params.set('wideFromVisitor', '1');
    }
    return Boolean(window.open('/?' + params));
  };

  window.fixture = {
    calls,
    openDirectory: () => send({type: 'tt:chat.openDirectory'}),
    visitorDeskAvailable: (detail) => send({type: 'tt:chat.visitorDeskAvailable', detail}),
    visitorDeskOpened: 0,
    authority: (status) => { authorityStatus = status; },
    holdList: () => { holdList = true; forceSync = true; send({type: 'tt:chat.changed'}); },
    incompleteList: () => { partialList = true; forceSync = true; send({type: 'tt:chat.changed'}); },
    reorderedList: () => { reorderedPages = true; forceSync = true; send({type: 'tt:chat.changed'}); },
    hasHeldList: () => !!heldList,
    hasReadIncompleteList: () => !partialList,
    hasReadReorderedPages: () => !reorderedPages,
    releaseList: () => { const release = heldList; heldList = undefined; release?.(); },
    archive: () => { archived = true; },
    receiveCat: () => {
      incoming = {
        id: 1004, seq: 4, conversation_id: 10, sender_id: contact.id, text: '🐱',
        created_at: '2026-06-15T08:05:00Z', revoked: false, reply_to_id: null
      };
      messages.push(incoming);
      send({type: 'tt:chat.changed'});
    },
    receiveOrderMessage: () => {
      incoming = {
        id: 1002, seq: 2, conversation_id: 10, sender_id: 32, text: '恢复后收到的订单消息',
        created_at: '2026-06-15T08:05:00Z', revoked: false, reply_to_id: null
      };
      messages.push(incoming);
      send({type: 'tt:chat.changed'});
    },
    jumpTo: (messageId) => send({type: 'tt:chat.openConversation', detail: {conversationId: 10, messageId}}),
    navigationReady: false,
    orderHistoryReady: false,
    historyActivated: 0,
    openedOrder: null,
    visible: (visible) => {
      document.querySelector('iframe').hidden = !visible;
      send({type: 'tt:chat.visible', detail: visible});
    }
  };

  addEventListener('message', (event) => {
    if(event.origin !== location.origin || event.source !== document.querySelector('iframe').contentWindow) return;
    const {type, id, path, method, body} = event.data;
    if(type === 'tt:chat.ready') fixture.visible(true);
    if(type === 'tt:chat.navigationReady') {
      fixture.navigationReady = true;
      if(conversationId) send({type: 'tt:chat.openConversation', detail: {conversationId}});
    }
    if(type === 'tt:chat.openWideWindow') fixture.wideFallback = event.data.detail;
    if(type === 'tt:chat.openVisitorDesk') fixture.visitorDeskOpened++;
    if(type === 'tt:order-history.ready') fixture.orderHistoryReady = true;
    if(type === 'tt:order-history.activate') fixture.historyActivated++;
    if(type === 'tt:chat.openOrder') fixture.openedOrder = event.data.detail;
    if(type !== 'tt:chat.request') return;

    calls.push({path, method, body});
    const url = new URL(path, location.origin);
    if(attachedImages.has(url.pathname)) return respond(id, attachedImages.get(url.pathname));
    if(authorityStatus !== 200 && (url.pathname === orderPath || url.pathname.includes('/conversations/10/'))) {
      return send({type: 'tt:chat.response', id, errorStatus: authorityStatus,
        error: authorityStatus === 403 ? '订单权限已撤销' : '暂时无法确认订单权限，请稍后重试'});
    }
    switch(url.pathname) {
    case orderPath + 'reopen/':
      if(!canReopen) return send({type: 'tt:chat.response', id, errorStatus: 403, error: '无重开权限'});
      archived = false;
      return respond(id, conversation(10, contact, messages.at(-1), read, messages.length - read));
    case orderPath:
      return respond(id, conversation(10, contact, messages.at(-1), 0, count));
    case orderPath + 'messages/':
      return respond(id, history(messages, url.searchParams));
    case '/api/auth/me':
      return respond(id, {user: self});
    case '/api/internal-chat/contacts/':
      return respond(id, {contacts: system ? [] : visitor ? [other] : [contact, other],
        worker_directory_only: !!visitor});
    case '/api/internal-chat/conversations/': {
      if(visitor && method === 'post' && body.peer_id === other.id) return respond(id, conversation(20, other, null, 0));
      if(reorderedPages) {
        const firstPage = Number(url.searchParams.get('offset')) === 0;
        reorderedPages = firstPage;
        return respond(id, {cursor: 0, conversations: [conversation(20, other, otherMessage, 1)], has_more: firstPage, next_offset: 1});
      }
      const unavailable = partialList || authorityStatus === 503;
      partialList = false;
      const result = {cursor: 0, ...(unavailable && {order_permission_unavailable: true}), conversations: [
        conversation(10, contact, messages.at(-1), read, messages.length - read),
        conversation(20, other, otherMessage, 1)
      ].filter(item => visitor && !wideFromVisitor ? item.id === 10 : item.kind !== 'order' || (!unavailable && authorityStatus === 200))};
      if(holdList) { holdList = false; heldList = () => respond(id, result); return; }
      return respond(id, result);
    }
    case '/api/internal-chat/sync/': {
      const changes = forceSync ? [{kind: 'conversation.read', conversation_id: 10, message: null}] : incoming && !Number(url.searchParams.get('after'))
        ? [{kind: 'message.created', conversation_id: 10, message: incoming}]
        : [];
      forceSync = false;
      return respond(id, {cursor: incoming ? 1 : 0, has_more: false,
        changes: authorityStatus === 503 ? [] : changes,
        ...(authorityMode && {order_visibility_token: authorityStatus === 503 ? null : authorityStatus === 200 ? 'allowed' : 'denied',
          order_permission_unavailable: authorityStatus === 503})});
    }
    case '/api/internal-chat/conversations/10/read/':
      // Keep the dialog's unread snapshot until the client receives another change.
      read = body.seq;
      return respond(id, {read_seq: read});
    case '/api/internal-chat/conversations/10/messages/':
    case '/api/internal-chat/conversations/20/messages/': {
      const rows = url.pathname.includes('/10/') ? messages : [otherMessage];
      if(method === 'post' && body.image) {
        const image = {mime: body.image.mime, width: 32, height: 32};
        const message = {...rows.at(-1), id: 2000 + rows.length, sender_id: self.id, seq: rows.length + 1,
          text: body.text, image, created_at: new Date().toISOString()};
        rows.push(message);
        attachedImages.set(url.pathname + message.id + '/image/', body.image);
        return respond(id, message);
      }
      const result = history(rows, url.searchParams);
      if(result) return respond(id, result);
      return send({type: 'tt:chat.response', id, errorStatus: 404, error: 'Not found'});
    }
    default:
      return respond(id, {});
    }
  });
}

const server = createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if(url.pathname === '/') {
    const scenario = {
      count: Number(url.searchParams.get('count')),
      h5: url.searchParams.has('h5'),
      group: url.searchParams.has('group'),
      system: url.searchParams.has('system'),
      authorityMode: url.searchParams.get('authority'),
      orderNumber: orderNo,
      historyOrderNo: url.searchParams.has('order-history') ? orderNo : null,
      copy: url.searchParams.has('copy'),
      visitor: url.searchParams.get('visitor'),
      wideFromVisitor: url.searchParams.has('wideFromVisitor'),
      conversationId: Number(url.searchParams.get('conversationId'))
    };
    const wide = url.searchParams.has('wide');
    const query = wide ? new URLSearchParams() : new URLSearchParams({list: '1'});
    if(scenario.h5) query.set('h5', '1');
    if(scenario.visitor) query.set('visitor', scenario.visitor);
    if(scenario.historyOrderNo) query.set('order-history', scenario.historyOrderNo);
    res.setHeader('Content-Type', mime['.html']);
    return res.end(
      `<!doctype html><script>(${installHost})(${JSON.stringify(scenario)})</script>` +
      `<iframe src="${prefix}?${query}" title="内部沟通" style="width:${wide || (scenario.h5 && scenario.wideFromVisitor) ? 1150 : 420}px;height:700px;border:0"></iframe>`
    );
  }

  if(!url.pathname.startsWith(prefix)) return res.writeHead(404).end();
  const filename = resolve(assetRoot, url.pathname.slice(prefix.length) || 'index.html');
  if(!filename.startsWith(assetRoot + sep) || !statSync(filename, {throwIfNoEntry: false})?.isFile()) {
    return res.writeHead(404).end();
  }
  res.setHeader('Content-Type', mime[extname(filename)] || 'application/octet-stream');
  createReadStream(filename).pipe(res);
});

async function check(browser, base, mode) {
  const page = await browser.newPage();
  try {
    await page.route('**/*', (route) => {
      return new URL(route.request().url()).origin === base ? route.continue() : route.abort();
    });
    const count = mode === 'empty' ? 0 : mode === 'wide-window-visitor-compact' ? 2 :
      ['older', 'jump'].includes(mode) ? 100 : ['switch-many', 'h5-unread', 'receive-and-reopen', 'copy-text'].includes(mode) ? 3 : 1;
    const readOnlyOrder = mode === 'order-history' || mode.startsWith('order-history-');
    const authority = mode.startsWith('authority-') || ['archive-reopen', 'archive-worker'].includes(mode);
    const group = mode === 'switch-group' || readOnlyOrder || authority;
    const system = mode === 'refund-system-notice' || mode === 'system-no-block';
    const params = new URLSearchParams({count: String(count)});
    if(group) params.set('group', '1');
    if(system) params.set('system', '1');
    if(authority || mode.startsWith('order-history-')) params.set('authority', mode);
    if(readOnlyOrder) params.set('order-history', '1');
    if(mode === 'h5-unread' || mode === 'visitor-directory') params.set('h5', '1');
    if(mode === 'copy-text') params.set('copy', '1');
    if(mode === 'wide-window') params.set('wide', '1');
    if(mode === 'authority-cold-link') {
      params.set('wide', '1');
      params.set('conversationId', '10');
    }
    if(mode === 'visitor-directory' || mode === 'wide-window-visitor-compact' || mode === 'visitor-image-formats') {
      params.set('visitor', 'guest');
    }
    if(mode === 'wide-window-visitor-compact') {
      params.set('h5', '1');
      params.set('wideFromVisitor', '1');
    }
    await page.goto(base + '/?' + params);

    const chat = page.frameLocator('iframe');
    const row = chat.locator(`.chatlist-chat[data-peer-id="${group ? -10 : 23}"]`).first();
    const otherRow = chat.locator('.chatlist-chat[data-peer-id="24"]').first();
    const back = chat.locator('.chat.active .sidebar-close-button').first();
    const bubble = (seq) => chat.locator(`.chat.active .bubble[data-mid="${1000 + seq}"]`);
    const calls = () => page.evaluate(() => fixture.calls);
    const historyPath = '/api/internal-chat/conversations/10/messages/';
    const historyRequests = async() => (await calls()).filter(({path}) => path.startsWith(historyPath));

    if(mode === 'image-formats' || mode === 'visitor-image-formats') {
      await row.click();
      const active = chat.locator('.chat.active');
      const editor = active.locator('.input-message-input[contenteditable="true"]').first();
      const popup = chat.locator('.popup-new-media');
      const posted = async() => (await calls()).filter(({path, method}) => path === historyPath && method === 'post');

      const selectImage = async(file) => {
        await active.getByRole('button', {name: '添加附件'}).click();
        const [chooser] = await Promise.all([
          page.waitForEvent('filechooser'),
          chat.getByRole('menuitem', {name: '图片', exact: true}).click()
        ]);
        await chooser.setFiles(file);
        await popup.getByRole('button', {name: '发送', exact: true}).click();
        await expect(popup).toBeHidden();
      };

      const base64 = await page.evaluate(() => {
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 900;
        const context = canvas.getContext('2d', {alpha: false});
        const image = context.createImageData(900, 900);
        let seed = 12345;
        for(let pixel = 0; pixel < image.data.length; pixel += 4) {
          seed ^= seed << 13;
          seed ^= seed >>> 17;
          seed ^= seed << 5;
          image.data[pixel] = seed & 255;
          image.data[pixel + 1] = (seed >>> 8) & 255;
          image.data[pixel + 2] = (seed >>> 16) & 255;
          image.data[pixel + 3] = 255;
        }
        context.putImageData(image, 0, 0);
        return canvas.toDataURL('image/png').split(',')[1];
      });
      const uncompressed = Buffer.from(base64, 'base64');
      expect(uncompressed.length).toBeGreaterThan(2 * 1024 * 1024);
      expect(uncompressed.length).toBeLessThan(3 * 1024 * 1024);
      await selectImage({name: 'screen.jpg', mimeType: 'image/jpeg', buffer: uncompressed});
      await expect.poll(async() => (await posted()).length).toBe(1);
      const first = (await posted())[0].body.image;
      expect(first.mime).toBe('image/png');
      expect(first.data === base64).toBe(true);

      const large = bitmap(1200, 1200);
      expect(large.length).toBeGreaterThan(3 * 1024 * 1024);
      await selectImage({name: 'screen.data', mimeType: 'application/octet-stream', buffer: large});
      await expect.poll(async() => (await posted()).length).toBe(2);
      const second = (await posted())[1].body.image;
      expect(second.mime).toMatch(/^image\/(jpeg|png|webp)$/);
      expect(Buffer.from(second.data, 'base64').length).toBeLessThanOrEqual(3 * 1024 * 1024);

      await editor.evaluate((node, base64) => {
        node.focus();
        const data = new DataTransfer();
        const bytes = Uint8Array.from(atob(base64), (character) => character.charCodeAt(0));
        data.items.add(new File([bytes], 'clipboard.jpg', {type: 'image/jpeg'}));
        node.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
      }, bitmap(32, 32).toString('base64'));
      await popup.getByRole('button', {name: '发送', exact: true}).click();
      await expect.poll(async() => (await posted()).length).toBe(3);
      const third = (await posted())[2].body.image;
      const signature = Buffer.from(third.data, 'base64').subarray(0, 2).toString();
      expect(third.mime).toMatch(/^image\/(jpeg|png|webp)$/);
      expect(signature).not.toBe('BM');
      return;
    }

    if(mode === 'copy-text') {
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], {origin: base});
      await row.click();
      const menu = chat.locator('.btn-menu.active');
      for(const seq of [2, 3]) {
        await bubble(seq).locator('.bubble-content').click({button: 'right'});
        await expect(menu).toBeVisible();
        await expect(menu.getByRole('menuitem', {name: '复制', exact: true})).toHaveCount(0);
        await page.keyboard.press('Escape');
      }
      await bubble(1).locator('.bubble-content').click({button: 'right'});
      await menu.getByRole('menuitem', {name: '复制', exact: true}).click();
      await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('第一行 <正文>\n  第二行 🐱');
      return;
    }

    if(mode.startsWith('wide-window') || mode === 'visitor-desk-menu') {
      const tools = chat.locator('.sidebar-tools-button.is-visible').first();
      const menu = chat.locator('.btn-menu.active');
      if(mode === 'visitor-desk-menu') {
        await expect.poll(() => page.evaluate(() => fixture.navigationReady)).toBe(true);
        await tools.click();
        await expect(menu.getByRole('menuitem', {name: '在新窗口打开'})).toBeVisible();
        await expect(menu.getByRole('menuitem', {name: '访客接待'})).toHaveCount(0);
        await page.keyboard.press('Escape');
        await page.evaluate(() => fixture.visitorDeskAvailable(true));
        await tools.click();
        await menu.getByRole('menuitem', {name: '访客接待'}).click();
        await expect.poll(() => page.evaluate(() => fixture.visitorDeskOpened)).toBe(1);
        await page.evaluate(() => fixture.visitorDeskAvailable(false));
        await tools.click();
        await expect(menu.getByRole('menuitem', {name: '在新窗口打开'})).toBeVisible();
        await expect(menu.getByRole('menuitem', {name: '访客接待'})).toHaveCount(0);
        return;
      }
      if(mode === 'wide-window-visitor-compact') {
        await otherRow.click();
        await expect(chat.locator('.chat.active .bubble[data-mid="150"]')).toBeVisible();
        await back.click();
      }
      await row.click();
      await expect(bubble(1)).toBeVisible();
      if(mode === 'wide-window-visitor-compact') {
        const current = chat.locator('.chat.active');
        await expect(current.locator('attach-menu-button.attach-file')).toHaveCount(0);
        await expect(current.getByRole('button', {name: '搜索', exact: true})).toHaveCount(0);
        await bubble(2).locator('.bubble-content').click({button: 'right'});
        await expect(menu.getByRole('menuitem', {name: '回复', exact: true})).toBeVisible();
        await expect(menu.getByRole('menuitem', {name: '复制', exact: true})).toBeVisible();
        await page.keyboard.press('Escape');
        await bubble(1).getByText('测试消息 1', {exact: true}).click({button: 'right'});
        await expect(menu.getByRole('menuitem', {name: '复制', exact: true})).toBeVisible();
        await expect(menu.getByRole('menuitem', {name: /删除|撤回|选择/})).toHaveCount(0);
        await page.keyboard.press('Escape');
        const ignoredImage = await current.locator('.input-message-input[contenteditable="true"]').first().evaluate((editor) => {
          const clipboard = new DataTransfer();
          clipboard.items.add(new File(['fixture'], 'test.png', {type: 'image/png'}));
          return !editor.dispatchEvent(new ClipboardEvent('paste', {clipboardData: clipboard, cancelable: true, bubbles: true}));
        });
        expect(ignoredImage).toBe(true);
      }
      if(mode.endsWith('-compact')) await back.click();
      if(mode === 'wide-window-visitor-compact') {
        await row.click({button: 'right'});
        await expect(menu).toBeVisible();
        await expect(menu.getByRole('menuitem', {name: '预览', exact: true})).toBeVisible();
        await expect(menu.getByRole('menuitem', {name: /置顶|标记为未读|归档|清空历史|删除|备注|静音|选择/})).toHaveCount(0);
        await page.keyboard.press('Escape');
      }
      await tools.click();
      const popupPromise = page.waitForEvent('popup');
      await menu.getByRole('menuitem', {name: '在新窗口打开'}).click();
      const popup = await popupPromise;
      expect(await page.evaluate(() => fixture.wide)).toEqual({conversationId: 10, fromClick: true});
      const wide = popup.frameLocator('iframe');
      await expect(wide.locator('.chatlist-chat[data-peer-id="23"]').first()).toBeVisible();
      await expect(wide.locator('.chat.active .bubble[data-mid="1001"]')).toBeVisible();
      await popup.close();
      await page.evaluate(() => { delete window.ttOpenChatWide; });
      await tools.click();
      await menu.getByRole('menuitem', {name: '在新窗口打开'}).click();
      await expect.poll(() => page.evaluate(() => fixture.wideFallback)).toEqual({conversationId: 10});
      return;
    }

    if(mode === 'visitor-directory') {
      await expect.poll(() => page.evaluate(() => fixture.navigationReady)).toBe(true);
      await page.evaluate(() => fixture.openDirectory());
      const directory = chat.getByRole('dialog', {name: '通讯录'});
      await expect(directory.getByRole('button', {name: '测试同事', exact: true})).toHaveCount(0);
      await directory.getByRole('button', {name: '另一位测试同事', exact: true}).click();
      await expect(directory).toHaveCount(0);
      await expect(chat.locator('.chat.active').getByText('另一位测试同事', {exact: true}).first()).toBeVisible();
      expect((await calls()).some(({method, body}) => method === 'post' && body?.peer_id === 24)).toBe(true);
      return;
    }

    if(system) {
      await chat.getByText('系统通知', {exact: true}).first().click();
      await expect(bubble(1)).toContainText('企微会话摘要：群内提出退款，原因待确认。');
      if(mode === 'system-no-block') {
        await chat.locator('.chat.active .user-title').click();
        const profile = chat.locator('#column-right .profile-content').last();
        await expect(profile.getByText('系统通知', {exact: true}).first()).toBeVisible();
        await expect(profile.getByText(/拉黑用户|取消屏蔽/)).toHaveCount(0);
        await page.reload();
        await otherRow.click();
        await expect(chat.locator('.chat.active .bubble[data-mid="150"]')).toBeVisible();
        await chat.locator('.chat.active .user-title').click();
        await expect(profile.getByText('另一位测试同事', {exact: true}).first()).toBeVisible();
        await profile.getByRole('button', {name: '更多', exact: true}).click();
        await expect(chat.locator('.btn-menu.active').getByText('拉黑用户', {exact: true})).toBeVisible();
        return;
      }
      await expect(chat.locator('.chat.active .input-message-input[contenteditable="true"]')).toBeHidden();
      await bubble(1).getByText('查看订单', {exact: true}).click();
      await expect.poll(() => page.evaluate(() => fixture.openedOrder)).toBe(orderNo);
      await expect.poll(async () => (await calls()).some(({path}) => path === '/api/internal-chat/conversations/10/read/')).toBe(true);
      return;
    }

    if(readOnlyOrder) {
      await expect.poll(() => page.evaluate(() => fixture.orderHistoryReady)).toBe(true);
      const popupSelector = '.popup-chat-preview.active:not(.hiding)';
      const popup = chat.locator(popupSelector);
      const message = popup.locator('.chat-preview .bubble[data-mid="1001"]');
      const summaryRequest = ['get', `/api/internal-chat/orders/${orderNo}/`];
      const latestRequest = ['get', `/api/internal-chat/orders/${orderNo}/messages/`];
      const requests = async() => (await calls()).map(({method, path}) => [method, path]);
      await expect(message).toContainText('测试消息 1');
      await expect(chat.locator('.tt-order-history-roster').getByText('打手 · 无权限', {exact: true})).toBeVisible();
      const originalPopup = await popup.elementHandle();
      const [initialSummary, ...initialHistory] = await requests();
      expect(initialSummary).toEqual(summaryRequest);
      expect(initialHistory.length).toBeGreaterThan(0);
      for (const request of initialHistory) expect(request).toEqual(latestRequest);
      const initialRequestCount = initialHistory.length + 1;

      await page.locator('iframe').evaluate((frame) => { frame.style.width = '460px'; });
      await expect.poll(() => originalPopup.evaluate((original, selector) => {
        const current = document.querySelector(selector);
        return current !== null && current !== original;
      }, popupSelector)).toBe(true);
      try {
        await expect(message).toBeInViewport();
      } catch(error) {
        console.error('Order history requests:', await requests());
        throw error;
      }
      await expect.poll(async() => (await requests()).length).toBeGreaterThan(initialRequestCount);
      const reopenedRequests = (await requests()).slice(initialRequestCount);
      for (const request of reopenedRequests) expect(request).toEqual(latestRequest);
      if(mode.startsWith('order-history-')) {
        const action = chat.locator('.tt-order-history-action button');
        await expect(action).toBeEnabled();
        await expect(action).toContainText(mode === 'order-history-join' ? '加入会话' : '打开会话');
        await action.click();
        await expect.poll(() => page.evaluate(() => fixture.historyActivated)).toBe(1);
      }
      return;
    }

    if(mode === 'empty') {
      await expect.poll(() => page.evaluate(() => fixture.navigationReady)).toBe(true);
      await page.evaluate(() => fixture.jumpTo());
      await expect(chat.locator('.chat.active .input-message-input[contenteditable="true"]').first()).toBeVisible();
      await expect.poll(async() => (await historyRequests()).map(({path}) => path)).toContain(historyPath);
      await expect(chat.locator('.chat.active .bubble').filter({hasText: '测试消息'})).toHaveCount(0);
      return;
    }

    if(mode === 'jump-latest') {
      await expect(row).toContainText('测试消息 1');
      await expect.poll(() => page.evaluate(() => fixture.navigationReady)).toBe(true);
      await page.evaluate(() => fixture.jumpTo(1001));
      await expect(bubble(1)).toBeInViewport();
      await expect(bubble(1)).toHaveClass(/\bis-highlighted\b/);
      await expect.poll(async() => (await historyRequests()).map(({path}) => path)).toContain(historyPath + '?around=1001');
      return;
    }

    if(mode === 'jump') {
      await expect(row).toContainText('测试消息 100');
      await row.click();
      await expect(bubble(100)).toBeVisible();
      await page.evaluate(() => fixture.jumpTo(1010));
      await expect(bubble(10)).toBeInViewport();
      await expect(bubble(10)).toHaveClass(/\bis-highlighted\b/);
      await expect.poll(async() => (await historyRequests()).map(({path}) => path)).toContain(historyPath + '?around=1010');
      return;
    }

    if(mode === 'authority-cold-link') {
      await expect.poll(async() => (await calls()).filter(({path}) => path.startsWith('/api/internal-chat/conversations/?')).length)
        .toBeGreaterThan(1);
      await expect(row).toHaveCount(0);
      await expect(chat.getByText('未找到该会话', {exact: true})).toHaveCount(0);
      await page.evaluate(() => fixture.authority(200));
      await expect(bubble(count)).toBeVisible({timeout: 10000});
      return;
    }
    await expect(row).toContainText('测试消息 ' + count);
    if(mode === 'h5-unread') expect((await calls()).some(({path}) => path.endsWith('/10/read/'))).toBe(false);
    if(mode === 'authority-unopened') {
      await page.evaluate(() => fixture.authority(403));
      await expect(row).toHaveCount(0, {timeout: 10000});
      await expect(otherRow).toBeVisible();
      await page.evaluate(() => fixture.authority(200));
      await expect(row).toBeVisible({timeout: 10000});
      await row.click();
      await expect(bubble(count)).toBeVisible();
      return;
    }
    await row.click();
    await expect(bubble(count)).toBeVisible();
    if(authority) {
      if(mode === 'authority-late-list') {
        await page.evaluate(() => fixture.holdList());
        await expect.poll(() => page.evaluate(() => fixture.hasHeldList())).toBe(true);
        await page.evaluate(() => fixture.authority(403));
        await expect(row).toHaveCount(0, {timeout: 10000});
        await page.evaluate(() => fixture.releaseList());
        await page.waitForTimeout(150);
        await expect(row).toHaveCount(0);
        await expect(otherRow).toBeVisible();
        return;
      }
      if(mode === 'authority-timeout') {
        const readAuthority = async() => (await calls()).filter(({path}) => path === `/api/internal-chat/orders/${orderNo}/`).length;
        const before = await readAuthority();
        await page.evaluate(() => { fixture.authority(503); fixture.receiveOrderMessage(); });
        await expect.poll(readAuthority).toBeGreaterThan(before);
        await page.waitForTimeout(300);
        await expect(bubble(count)).toBeVisible();
        await expect(row).toHaveCount(1);
        await expect(chat.getByText('订单权限已撤销', {exact: true})).toHaveCount(0);
        await page.evaluate(() => fixture.authority(200));
        await expect(bubble(2)).toContainText('恢复后收到的订单消息', {timeout: 10000});
        await expect(bubble(count)).toBeVisible();
        return;
      }
      if(mode === 'authority-partial-list') {
        const listCount = async() => (await calls()).filter(({path}) => path.startsWith('/api/internal-chat/conversations/?')).length;
        const before = await listCount();
        await page.evaluate(() => fixture.incompleteList());
        await expect.poll(() => page.evaluate(() => fixture.hasReadIncompleteList())).toBe(true);
        await expect.poll(listCount).toBeGreaterThan(before + 1);
        await expect(bubble(count)).toBeVisible();
        await expect(row).toHaveCount(1);
        await expect(chat.getByText('订单权限已撤销', {exact: true})).toHaveCount(0);
        return;
      }
      if(mode === 'authority-reordered-pages') {
        await page.evaluate(() => fixture.reorderedList());
        await expect.poll(() => page.evaluate(() => fixture.hasReadReorderedPages())).toBe(true);
        await expect(bubble(count)).toBeVisible();
        await expect(row).toHaveCount(1);
        await expect(chat.getByText('订单权限已撤销', {exact: true})).toHaveCount(0);
        return;
      }
      if(mode === 'authority-revoke') {
        await page.evaluate(() => fixture.authority(403));
        await expect(bubble(count)).toHaveCount(0, {timeout: 10000});
        await expect(row).toHaveCount(0);
        await expect(chat.getByText('订单权限已撤销', {exact: true}).first()).toBeVisible();
        await otherRow.click();
        await expect(chat.locator('.chat.active .bubble[data-mid="150"]')).toBeVisible();
        await page.evaluate(() => fixture.authority(200));
        await expect(row).toBeVisible({timeout: 10000});
        return;
      }
      await page.evaluate(() => fixture.archive());
      const archive = chat.locator('.chat.active .preview-order-archive');
      await expect(archive).toContainText('已归档', {timeout: 10000});
      await expect(bubble(count)).toBeVisible();
      await expect(chat.locator('.chat.active .input-message-input[contenteditable="true"]')).toBeHidden();
      const reopen = archive.getByRole('button', {name: '重开群聊'});
      if(mode === 'archive-worker') { await expect(reopen).toHaveCount(0); return; }
      await reopen.click();
      await expect(archive).toHaveCount(0);
      await expect(chat.locator('.chat.active .input-message-input[contenteditable="true"]').first()).toBeVisible();
      expect((await calls()).some(({path, method}) => path === `/api/internal-chat/orders/${orderNo}/reopen/` && method === 'post')).toBe(true);
      return;
    }
    if(mode === 'older') {
      await bubble(count).evaluate((element) => {
        const scrollable = element.closest('.scrollable-y');
        scrollable.scrollTop = 0;
        scrollable.dispatchEvent(new Event('scroll'));
      });
      await expect.poll(async() => (await historyRequests()).map(({path}) => path)).toContain(historyPath + '?before=41');
      await expect(bubble(40)).toBeVisible();
      await expect(bubble(count)).toHaveCount(1);
      return;
    }

    if(mode === 'h5-unread') {
      await expect(bubble(count)).toBeInViewport();
      await expect.poll(async() => (await historyRequests()).map(({path}) => path)).toContain(historyPath + '?around=1003');
      await expect.poll(async() => (await calls()).some(({path, body}) => path.endsWith('/10/read/') && body.seq === count)).toBe(true);
      return;
    }

    if(mode === 'hide-show') {
      await page.evaluate(() => fixture.visible(false));
      await expect(page.locator('iframe')).toBeHidden();
      await page.evaluate(() => fixture.visible(true));
      await expect(bubble(count)).toBeVisible();
      await expect(bubble(count)).toHaveCount(1);
      return;
    }

    let newest = count;
    if(mode === 'receive-and-reopen') {
      await back.click();
      await page.evaluate(() => fixture.receiveCat());
      await expect.poll(() => row.evaluate((element) => element.textContent.includes('🐱') ||
        !!element.querySelector('[alt="🐱"],[data-emoji="🐱"]'))).toBe(true);
      await row.click();
      newest = count + 1;
      await expect(bubble(newest)).toBeInViewport();
      await expect.poll(async() => (await calls()).some(({path, body}) =>
        path.endsWith('/10/read/') && body.seq === newest)).toBe(true);
    }

    await back.click();
    await otherRow.click();
    await expect(chat.locator('.chat.active').getByText('另一位测试同事', {exact: true}).first()).toBeVisible();
    await back.click();
    await row.click();
    try {
      await expect(bubble(newest)).toBeInViewport({timeout: 2500});
    } catch(error) {
      console.error('Reopen history requests:', (await historyRequests()).map(({path}) => path));
      throw error;
    }
    await expect(bubble(newest)).toHaveCount(1);
    if(count > 1) await expect(bubble(1)).toBeVisible();
  } catch(error) {
    if(process.env.TT_WEBK_TEST_ARTIFACTS) {
      const target = process.env.TT_WEBK_TEST_ARTIFACTS + '/' + mode;
      await page.screenshot({path: target + '.png'});
      require('node:fs').writeFileSync(target + '.html', await page.frameLocator('iframe').locator('body').innerHTML());
    }
    throw error;
  } finally {
    await page.close();
  }
}

async function run() {
  const modes = ['switch-one', 'switch-many', 'switch-group', 'hide-show', 'older', 'jump', 'jump-latest', 'h5-unread', 'empty', 'refund-system-notice', 'system-no-block', 'order-history', 'receive-and-reopen', 'authority-revoke', 'authority-timeout', 'authority-partial-list', 'authority-reordered-pages', 'authority-cold-link', 'authority-unopened', 'authority-late-list', 'archive-reopen', 'archive-worker', 'order-history-member', 'order-history-archived-member', 'order-history-join', 'copy-text', 'wide-window', 'wide-window-compact', 'wide-window-visitor-compact', 'visitor-directory', 'visitor-desk-menu', 'image-formats', 'visitor-image-formats'];
  const requested = process.argv.slice(3);
  for (const mode of requested) {
    if(!modes.includes(mode)) throw new Error('Unknown scenario: ' + mode);
  }
  const channel = process.env.TT_WEBK_BROWSER_CHANNEL;
  const browser = await chromium.launch(channel ? {channel} : {});
  try {
    await new Promise((ready) => server.listen(0, '127.0.0.1', ready));
    const base = 'http://127.0.0.1:' + server.address().port;
    for (const mode of requested.length ? requested : modes) {
      await check(browser, base, mode);
      console.log('Web K reopen-history:', mode, 'passed');
    }
  } finally {
    await browser.close();
    server.close();
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
