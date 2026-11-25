const login = document.querySelector('#login');
const status = document.querySelector('#status');
const identity = document.querySelector('#identity');
const logout = document.querySelector('#logout');
const host = document.querySelector('#chat-host');
const errorMessage = document.querySelector('#login-error');
const base = '/api/internal-chat/';
let frame;
let poll;

function approvedPath(method, path) {
  if (typeof path !== 'string' || !path.startsWith('/api/') || path.includes('#')) return false;
  const parsed = new URL(path, location.origin);
  if (parsed.origin !== location.origin || parsed.pathname !== path.split('?')[0]) return false;
  const name = parsed.pathname;
  if (method === 'get') {
    return name === '/api/auth/me' || name === `${base}contacts/` || name === `${base}conversations/` || name === `${base}sync/` ||
      /^\/api\/internal-chat\/conversations\/[1-9]\d*\/messages\/(?:search\/|[1-9]\d*\/image\/)?$/.test(name);
  }
  return method === 'post' && (name === `${base}conversations/` ||
    /^\/api\/internal-chat\/conversations\/[1-9]\d*\/(?:messages\/|read\/|preferences\/|pin\/|messages\/[1-9]\d*\/revoke\/)$/.test(name));
}

async function request(path, method = 'get', body) {
  const response = await fetch(path, {
    method: method.toUpperCase(),
    credentials: 'same-origin',
    cache: 'no-store',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let result;
  try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) {
    const error = new Error(result.error || '聊天请求失败，请稍后再试');
    error.status = response.status;
    throw error;
  }
  return result;
}

function notify(type, detail) {
  frame?.contentWindow?.postMessage({type, detail}, location.origin);
}

function changed() {
  notify('tt:chat.changed');
}

function showLogin() {
  clearInterval(poll);
  frame?.remove();
  frame = undefined;
  identity.textContent = '';
  logout.hidden = true;
  login.hidden = false;
  status.textContent = '仅模拟工作室';
}

function openChat(user) {
  clearInterval(poll);
  frame?.remove();
  login.hidden = true;
  logout.hidden = false;
  identity.textContent = user.display_name || user.name || `账号 ${user.id}`;
  status.textContent = '独立聊天后端 · 测试中';
  frame = document.createElement('iframe');
  frame.title = '团团通内部沟通';
  frame.src = '/internal-chat-webk/?local-chat=1&noServiceWorker=1&list=1&live=1';
  host.append(frame);
  poll = setInterval(changed, 1000);
}

window.addEventListener('message', async (event) => {
  const current = frame;
  if (!current || event.source !== current.contentWindow || event.origin !== location.origin) return;
  const data = event.data;
  if (data?.type === 'tt:chat.ready') {
    notify('tt:chat.visible', true);
    changed();
    return;
  }
  if (data?.type === 'tt:chat.closeFromList') {
    status.textContent = '独立聊天后端 · 测试中';
    return;
  }
  if (data?.type !== 'tt:chat.request' || !Number.isSafeInteger(data.id) || data.id < 1) return;
  try {
    if (!approvedPath(data.method, data.path)) throw new Error('不支持该聊天请求');
    const result = await request(data.path, data.method, data.body);
    if (frame !== current) return;
    current.contentWindow.postMessage({type: 'tt:chat.response', id: data.id, result}, location.origin);
    if (data.method === 'post') changed();
  } catch (error) {
    if (frame !== current) return;
    current.contentWindow.postMessage({type: 'tt:chat.response', id: data.id, error: error.message, errorStatus: error.status}, location.origin);
    if (error.status === 401) showLogin();
  }
});

login.addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = login.querySelector('button');
  submit.disabled = true;
  errorMessage.textContent = '';
  try {
    const data = new FormData(login);
    const result = await request('/pilot/login', 'post', {username: data.get('username'), password: data.get('password')});
    login.reset();
    openChat(result.user);
  } catch (error) {
    errorMessage.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

logout.addEventListener('click', async () => {
  logout.disabled = true;
  try {
    await request('/pilot/logout', 'post', {});
    showLogin();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    logout.disabled = false;
  }
});

window.addEventListener('online', () => { status.textContent = '连接已恢复，正在补齐消息'; changed(); });
window.addEventListener('offline', () => { status.textContent = '网络已断开，恢复后补齐消息'; });
window.addEventListener('focus', () => { notify('tt:chat.visible', true); changed(); });
document.addEventListener('visibilitychange', () => {
  notify('tt:chat.visible', !document.hidden);
  if (!document.hidden) changed();
});
window.addEventListener('pagehide', () => clearInterval(poll));

try {
  const result = await request('/pilot/session');
  if (result.user) openChat(result.user);
  else showLogin();
} catch (error) {
  showLogin();
  if (error.status !== 401) errorMessage.textContent = error.message;
}
