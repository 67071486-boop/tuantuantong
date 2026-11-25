const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const frontendRequire = createRequire(path.resolve(__dirname, '../../frontend/package.json'));
const ts = frontendRequire('typescript');
const sourceFile = process.argv[2];
if(!sourceFile) throw Error('Usage: node authority-transport.cjs <applied Web K liveTransport.ts>');

async function run() {
  const origin = 'https://chat.test.invalid';
  let receive;
  const calls = [];
  const parent = {location: {origin}, postMessage: value => calls.push(value)};
  const window = {parent, setTimeout, clearTimeout, addEventListener: (_type, handler) => { receive = handler; }};
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(sourceFile, 'utf8')
    .replaceAll('import.meta.env.VITE_TTT_CHAT_HOSTED', "'1'"),
  {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
  vm.runInNewContext(code, {exports, window, document: {hidden: false}, location: {origin},
    EventTarget, Event, CustomEvent, URLSearchParams, URL, Map, Number, Promise});
  const api = exports.createLiveTransport();
  const reply = (id, value) => receive({origin, source: parent,
    data: {type: 'tt:chat.response', id, ...value}});
  const requestId = () => calls.at(-1).id;
  const list = api.conversations();
  const listId = requestId();
  const denied = api.history(10).catch(error => error);
  reply(requestId(), {errorStatus: 403, error: '订单权限已撤销'});
  assert.equal((await denied).status, 403);
  reply(listId, {result: {cursor: 9, conversations: [
    {id: 10, kind: 'order'}, {id: 20, kind: 'direct'},
  ]}});
  assert.deepEqual(Array.from((await list).conversations, row => row.id), [20],
    'A list issued before revocation must not restore the revoked group; direct chat remains');

  const fresh = api.conversations();
  reply(requestId(), {result: {cursor: 9, conversations: [{id: 10, kind: 'order'}]}});
  assert.deepEqual(Array.from((await fresh).conversations, row => row.id), [10],
    'A newly authorized list can restore the same group without advancing the cursor');

  const image = api.image(10, 31).catch(error => error);
  const imageId = requestId();
  const unavailable = api.history(10).catch(error => error);
  reply(requestId(), {errorStatus: 503, error: '暂时无法确认订单权限，请稍后重试'});
  assert.equal((await unavailable).status, 503);
  reply(imageId, {result: {mime: 'image/png', data: 'LATE_IMAGE'}});
  assert.equal((await image).status, 503, 'An image requested before invalidation cannot be displayed afterwards');
  const pages = api.conversations();
  assert.match(calls.at(-1).path, /limit=100&offset=0$/);
  reply(requestId(), {result: {cursor: 9, has_more: true, next_offset: 100,
    conversations: Array.from({length: 100}, (_, i) => ({id: i + 100, kind: 'direct'}))}});
  await new Promise(setImmediate);
  assert.match(calls.at(-1).path, /limit=100&offset=100$/);
  reply(requestId(), {result: {cursor: 10, has_more: false, next_offset: null,
    conversations: [{id: 200, kind: 'direct'}, {id: 201, kind: 'direct'}]}});
  const all = await pages;
  assert.equal(all.conversations.length, 102, 'All pages must be collected, including the final short page');
  assert.equal(all.cursor, 9, 'Keep the earliest page cursor so concurrent changes are not skipped');
  const invalid = api.conversations().catch(error => error);
  reply(requestId(), {result: {conversations: [], has_more: true, next_offset: 0}});
  assert.match((await invalid).message, /会话分页返回异常/);
  console.log('Web K authority transport: late list/image rejection, direct chat preservation and fresh recovery passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
