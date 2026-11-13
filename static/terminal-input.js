// Ordered, bounded keystroke batches. Each batch retains its original session.
(() => {
  'use strict';
  class SSHTerminalInput {
    constructor(send, onError) {
      this.send = send;
      this.onError = onError;
      this.queue = [];
      this.timer = null;
      this.sending = false;
      this.blocked = false;
    }

    write(sessionId, data) {
      if (this.blocked || !sessionId || !data) return;
      for (let offset = 0; offset < data.length;) {
        const last = this.queue.at(-1);
        const item = last?.sessionId === sessionId && last.data.length < 4096
          ? last : { sessionId, data: '' };
        if (item !== last) this.queue.push(item);
        let end = Math.min(data.length, offset + 4096 - item.data.length);
        // Never split a UTF-16 surrogate pair across encrypted requests.
        if (end < data.length && /[\uD800-\uDBFF]/.test(data[end - 1])) end--;
        if (end === offset) { item.data += data.slice(offset, offset + 2); offset += 2; }
        else { item.data += data.slice(offset, end); offset = end; }
      }
      if (!this.sending && this.timer === null) {
        this.timer = setTimeout(() => { this.timer = null; this.drain(); }, 0);
      }
    }

    async drain() {
      if (this.sending) return;
      this.sending = true;
      try {
        while (this.queue.length && !this.blocked) {
          const item = this.queue.shift();
          await this.send(item.sessionId, item.data);
        }
      } catch (error) {
        this.queue = [];
        this.blocked = true;
        this.onError(error);
      } finally {
        this.sending = false;
      }
    }

    resume() { this.blocked = false; }
  }
  window.SSHTerminalInput = SSHTerminalInput;
})();
