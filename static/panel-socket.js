// Persistent local transport. A failed input is never automatically replayed.
(() => {
  'use strict';
  class SSHPanelSocket {
    constructor(descriptor, { onState = () => {}, socketFactory = url => new WebSocket(url) } = {}) {
      this.descriptor = descriptor;
      this.onState = onState;
      this.socketFactory = socketFactory;
      this.pending = new Map();
      this.listeners = new Set();
      this.nextId = 0;
      this.ready = false;
      this.opening = null;
    }

    connect() {
      if (this.ready) return Promise.resolve();
      if (this.opening) return this.opening;
      const { url, token } = this.descriptor;
      if (!/^ws:\/\/127\.0\.0\.1:\d+\/terminal$/.test(url) || !token) {
        return Promise.reject(new Error('实时通道地址无效'));
      }
      this.opening = new Promise((resolve, reject) => {
        const socket = this.socketFactory(url);
        this.socket = socket;
        let settled = false;
        const timer = setTimeout(() => { fail(); socket.close(); }, 4000);
        const fail = () => {
          if (this.socket !== socket) return;
          clearTimeout(timer);
          this.ready = false;
          this.opening = null;
          if (!settled) { settled = true; reject(new Error('实时通道暂不可用')); }
          for (const item of this.pending.values()) {
            clearTimeout(item.timer);
            item.reject(new Error('连接中断，输入未重发；请检查终端'));
          }
          this.pending.clear();
          this.onState(false);
          this.listeners.forEach(listener => listener({ type: 'disconnected' }));
        };
        socket.onopen = () => socket.send(JSON.stringify({ type: 'authenticate', token }));
        socket.onmessage = message => {
          let value;
          try { value = JSON.parse(message.data); } catch { return; }
          if (value.type === 'ready') {
            clearTimeout(timer);
            this.ready = true;
            this.onState(true);
            settled = true;
            resolve();
          } else if (value.id != null) {
            const item = this.pending.get(value.id);
            if (!item) return;
            this.pending.delete(value.id);
            clearTimeout(item.timer);
            if (value.error) item.reject(new Error(value.error));
            else item.resolve(value.result);
          } else this.listeners.forEach(listener => listener(value));
        };
        socket.onerror = fail;
        socket.onclose = fail;
      });
      return this.opening;
    }

    async request(op, args = {}) {
      await this.connect();
      return new Promise((resolve, reject) => {
        const id = ++this.nextId;
        const timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error('操作超时，输入未重发；请检查终端'));
        }, 12000);
        this.pending.set(id, { resolve, reject, timer });
        try { this.socket.send(JSON.stringify({ id, op, ...args })); }
        catch { clearTimeout(timer); this.pending.delete(id); reject(new Error('输入未发送：连接已断开')); }
      });
    }

    listen(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }

    close() { this.socket?.close(); }
  }
  window.SSHPanelSocket = SSHPanelSocket;
})();
