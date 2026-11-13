// Injected only into the MCP App resource. The standalone page is unchanged.
(() => {
  'use strict';
  const pending = new Map();
  let nextId = 0;
  let connection = null;
  let booting = null;
  let socket = null;
  let realtimeRetryAt = 0;
  const banner = document.createElement('div');
  banner.className = 'native-banner';
  banner.textContent = '正在准备 SSH 服务…';
  document.querySelector('header').after(banner);

  const style = document.createElement('style');
  style.textContent = `
    .app { grid-template-rows: auto auto minmax(0, 1fr) auto; }
    .native-banner { padding: 8px 14px; font: 12px system-ui; color: #486375; }
    .native-banner button { margin-left: 10px; }
    html, body { height: 100%; margin: 0; }
  `;
  document.head.appendChild(style);
  document.title = 'SSH 终端';

  function rpc(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++nextId;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error('Codex 面板连接超时，请重新打开面板。'));
      }, 25000);
      pending.set(id, { resolve, reject, timer });
      window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
    });
  }

  function acceptConnection(result) {
    const value = result?._meta?.sshBridge;
    if (value?.transport !== 'mcp' || !value.encryption?.publicKey || !value.encryption?.keyId) {
      throw new Error('SSH 面板的本机连接信息无效。');
    }
    connection = value;
    if (socket?.ready && socket.descriptor.url === value.realtime?.url
        && socket.descriptor.token === value.realtime?.token) return;
    const previousSocket = socket;
    socket = null;
    previousSocket?.close();
    realtimeRetryAt = 0;
    if (value.realtime) {
      const newSocket = new window.SSHPanelSocket(value.realtime, { onState: connected => {
        if (socket !== newSocket) return;
        banner.textContent = !value.privateClipboard
          ? 'SSH 面板已更新，后台仍是旧版：请刷新插件连接后重新打开面板'
          : connected
          ? '实时连接 · 右键粘贴 · Ctrl+Shift+C 复制 · Ctrl+Shift+V 粘贴'
          : '兼容连接 · 右键 / Ctrl+V 粘贴可用 · 底部输入框可发送命令';
      } });
      socket = newSocket;
    }
  }

  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.data?.jsonrpc !== '2.0') return;
    const message = event.data;
    const request = pending.get(message.id);
    if (request) {
      clearTimeout(request.timer);
      pending.delete(message.id);
      if (message.error) request.reject(new Error('Codex 面板调用失败，请重试。'));
      else request.resolve(message.result);
    }
    if (message.method === 'ui/resource-teardown' && message.id != null) {
      window.parent.postMessage({ jsonrpc: '2.0', id: message.id, result: {} }, '*');
    }
  });

  const initialized = rpc('ui/initialize', {
    protocolVersion: '2026-01-26',
    appInfo: { name: 'codex-ssh-terminal', version: '0.9.1' },
    appCapabilities: {},
  }).then(result => {
    window.parent.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, '*');
    return result;
  });

  function showError(message = 'SSH 面板连接失败。') {
    banner.textContent = message;
    const retry = document.createElement('button');
    retry.textContent = '重新连接服务';
    retry.addEventListener('click', () => bootstrap(true).catch(() => {}));
    banner.appendChild(retry);
  }

  async function bootstrap(force = false) {
    if (booting) return booting;
    if (connection && !force) return connection;
    booting = (async () => {
      banner.textContent = '正在准备 SSH 服务…';
      await initialized;
      if (!connection || force) {
        const result = await rpc('tools/call', { name: 'panel_bootstrap', arguments: {} });
        if (result?.isError) throw new Error('SSH 服务启动失败，请重新连接服务。');
        acceptConnection(result);
      }
      if (!connection) throw new Error('没有收到 SSH 服务连接信息。');
      banner.textContent = '四机联动 · 点击终端输入 · 右键粘贴';
      return connection;
    })();
    try { return await booting; }
    catch (error) { showError(); throw error; }
    finally { booting = null; }
  }

  function base64(bytes) {
    return btoa(Array.from(new Uint8Array(bytes), byte => String.fromCharCode(byte)).join(''));
  }

  async function encryptInput(path, payload, descriptor) {
    const publicKey = await crypto.subtle.importKey(
      'spki', Uint8Array.from(atob(descriptor.publicKey), char => char.charCodeAt(0)),
      { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt'],
    );
    const aesKey = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt']);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const plaintext = new TextEncoder().encode(JSON.stringify({
      path, payload, messageId: crypto.randomUUID(), expiresAt: Date.now() + 60000,
    }));
    const ciphertext = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(descriptor.keyId) },
      aesKey, plaintext,
    );
    const wrappedKey = await crypto.subtle.encrypt(
      { name: 'RSA-OAEP' }, publicKey, await crypto.subtle.exportKey('raw', aesKey),
    );
    return { keyId: descriptor.keyId, wrappedKey: base64(wrappedKey), iv: base64(iv), ciphertext: base64(ciphertext) };
  }

  async function realtime() {
    await bootstrap();
    const candidate = socket;
    if (!candidate || Date.now() < realtimeRetryAt) return null;
    try {
      await candidate.connect();
      return socket === candidate ? candidate : null;
    } catch {
      if (socket === candidate) realtimeRetryAt = Date.now() + 30000;
      return null;
    }
  }

  function privateInputError(result) {
    try {
      const block = result.content?.find(item => item.type === 'text');
      return JSON.parse(block?.text || '{}');
    } catch {
      return {};
    }
  }

  async function privateRequest(path, payload) {
    const previous = await bootstrap();
    const send = async current => rpc('tools/call', {
      name: 'panel_private_input',
      arguments: { envelope: await encryptInput(path, payload, current.encryption) },
    });
    let result = await send(previous);
    if (result?.isError) {
      const failure = privateInputError(result);
      const rejectedBeforeDispatch = failure?.code === 'PANEL_KEY_EXPIRED'
        || failure?.error === 'Initialize the SSH panel before entering private input.';
      const legacyRejection = failure?.error === 'Private input is invalid, expired, or already used. Enter it again.';
      if (rejectedBeforeDispatch || legacyRejection) {
        const refreshed = await bootstrap(connection === previous);
        if (refreshed.encryption.keyId !== previous.encryption.keyId) {
          // Only a definitive pre-dispatch rejection permits sending again.
          if (!rejectedBeforeDispatch) {
            throw new Error('面板输入连接已刷新。请检查终端后重新输入。');
          }
          result = await send(refreshed);
        }
      }
    }
    if (result?.isError) {
      showError('面板输入操作未完成。');
      throw new Error('面板未确认此次操作，请检查当前状态后重试。');
    }
    const data = result?._meta?.payload;
    if (data == null) throw new Error('SSH 服务未返回输入结果。请检查终端内容。');
    return data;
  }

  async function bridgeFetch(path, options = {}) {
    if (typeof path !== 'string' || !path.startsWith('/api/')) {
      throw new Error('Unsupported SSH panel request.');
    }
    const method = options.method || 'GET';
    const payload = options.body ? JSON.parse(options.body) : {};
    const direct = await realtime();
    if (direct) {
      // Once sent, a mutation must not be retried via MCP: it may have run.
      const data = await direct.request('request', { method, path, payload });
      return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
    }
    if (path === '/api/input' || path === '/api/upload-input') {
      const data = await privateRequest(path, payload);
      return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
    }
    const result = await rpc('tools/call', { name: 'panel_request', arguments: { method, path, payload } });
    if (result?.isError) {
      showError();
      throw new Error('SSH 面板操作失败，请重新连接服务后重试。');
    }
    const data = result?._meta?.payload;
    if (data == null) throw new Error('SSH 服务未返回数据。');
    return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
  }

  function stream(sessionId, onEvent, onError, initialAfter = 0) {
    let closed = false;
    let timer;
    let after = initialAfter;
    let polling = false;
    let wakeRequested = false;
    let removeListener = null;
    let directStream = null;
    const controller = new AbortController();
    async function poll() {
      if (closed || polling) return;
      polling = true;
      wakeRequested = false;
      try {
        const data = await bridgeFetch(
          `/api/events?sessionId=${encodeURIComponent(sessionId)}&after=${after}&limit=0`,
          { signal: controller.signal },
        ).then(response => response.json());
        if (closed) return;
        for (const event of data.events || []) {
          if (event.seq > after) { after = event.seq; onEvent(event); }
        }
      } catch {
        if (!closed) onError();
      } finally {
        polling = false;
        if (!closed) timer = setTimeout(poll, wakeRequested ? 0 : document.hidden ? 1500 : 100);
      }
    }
    (async () => {
      const direct = await realtime();
      if (closed) return;
      if (!direct) { poll(); return; }
      directStream = direct;
      removeListener = direct.listen(value => {
        if (closed) return;
        if (value.type === 'event' && value.sessionId === sessionId && value.event.seq > after) {
          after = value.event.seq;
          onEvent(value.event);
        } else if (value.type === 'disconnected'
            || value.type === 'stream-error' && value.sessionId === sessionId) {
          directStream = null;
          removeListener?.();
          onError();
          poll();
        }
      });
      try { await direct.request('subscribe', { sessionId, after }); }
      catch { removeListener?.(); directStream = null; if (!closed) poll(); }
    })();
    return {
      close() {
        closed = true; clearTimeout(timer); controller.abort(); removeListener?.();
        if (directStream?.ready) directStream.request('unsubscribe').catch(() => {});
      },
      wake() {
        if (closed || directStream) return;
        if (polling) { wakeRequested = true; return; }
        clearTimeout(timer);
        poll();
      },
    };
  }

  function notify(message) {
    banner.textContent = String(message);
  }

  async function clipboardRequest(operation, text) {
    const current = await bootstrap();
    if (!current.privateClipboard) throw new Error('SSH 后台仍是旧版，请刷新 SSH 插件连接，或方便时重开 Codex；无需断开服务器。');
    const replyBytes = crypto.getRandomValues(new Uint8Array(32));
    const payload = operation === 'read'
      ? { operation, responseKey: base64(replyBytes) } : { operation, text };
    // This encrypted MCP path also works when the optional WebSocket is blocked.
    // Clipboard text and the reply key never appear in plaintext tool messages.
    const result = await privateRequest('/panel/clipboard', payload);
    if (operation === 'write') return;
    const response = result.encryptedClipboard;
    if (!response) throw new Error('剪贴板通道尚未更新，请刷新 SSH 插件连接。');
    const replyKey = await crypto.subtle.importKey('raw', replyBytes, 'AES-GCM', false, ['decrypt']);
    const plain = await crypto.subtle.decrypt({
      name: 'AES-GCM',
      iv: Uint8Array.from(atob(response.iv), char => char.charCodeAt(0)),
      additionalData: new TextEncoder().encode('codex-ssh-clipboard-v1'),
    }, replyKey, Uint8Array.from(atob(response.ciphertext), char => char.charCodeAt(0)));
    return JSON.parse(new TextDecoder().decode(plain)).text;
  }

  function confirmAction(message) {
    return new Promise(resolve => {
      const dialog = document.createElement('dialog');
      const text = document.createElement('p');
      text.textContent = message;
      const cancel = document.createElement('button');
      const accept = document.createElement('button');
      cancel.textContent = '取消';
      accept.textContent = '确认';
      dialog.append(text, cancel, accept);
      document.body.appendChild(dialog);
      const finish = value => { dialog.close(); dialog.remove(); resolve(value); };
      cancel.addEventListener('click', () => finish(false));
      accept.addEventListener('click', () => finish(true));
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
      dialog.showModal();
    });
  }
  window.alert = notify;
  window.codexSSHNative = {
    fetch: bridgeFetch, stream, confirm: confirmAction,
    readClipboard: () => clipboardRequest('read'),
    writeClipboard: text => clipboardRequest('write', text),
  };
})();
