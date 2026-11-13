// Independent command entry for a terminal whose focus/input handling is broken.
(() => {
  'use strict';
  class SSHFallbackInput {
    constructor({ input, execute, raw, paste, label, status, send, readClipboard }) {
      Object.assign(this, { input, execute, raw, paste, label, status, send, readClipboard });
      this.drafts = new Map();
      this.sessionId = '';
      this.enabled = false;
      this.sending = false;
      this.pasting = false;
      execute.addEventListener('click', () => this.submit(true));
      raw.addEventListener('click', () => this.submit(false));
      paste.addEventListener('click', () => this.pasteDraft());
      input.addEventListener('keydown', event => {
        if (event.isComposing || event.keyCode === 229) return;
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault(); this.submit(true);
        } else if (window.codexSSHNative && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v') {
          event.preventDefault(); this.pasteDraft();
        }
      });
    }

    sync(session, enabled) {
      const id = session?.sessionId || '';
      if (id !== this.sessionId) {
        if (this.sessionId) this.drafts.set(this.sessionId, this.input.value);
        this.sessionId = id;
        this.input.value = this.drafts.get(id) || '';
        this.status.textContent = '';
      }
      this.enabled = Boolean(enabled);
      this.label.textContent = session ? `备用输入 → ${session.profileName || id}` : '备用命令输入';
      this.input.disabled = !this.enabled;
      this.execute.disabled = this.raw.disabled = !this.enabled || this.sending;
      this.paste.disabled = !this.enabled || this.pasting;
    }

    async pasteDraft() {
      if (!this.enabled || this.pasting) return;
      const id = this.sessionId, previous = this.input.value;
      const start = this.input.selectionStart, end = this.input.selectionEnd;
      this.pasting = true; this.paste.disabled = true;
      try {
        const text = await this.readClipboard();
        if (id !== this.sessionId || previous !== this.input.value || !this.enabled) return;
        if (typeof text !== 'string') throw new Error('剪贴板没有文本');
        this.input.setRangeText(text, start, end, 'end');
        this.input.focus();
        this.status.textContent = '已粘贴到输入框，尚未发送';
      } catch (error) { this.status.textContent = error.message; }
      finally { this.pasting = false; this.paste.disabled = !this.enabled; }
    }

    async submit(enter) {
      if (!this.enabled || this.sending) return;
      const id = this.sessionId, original = this.input.value;
      if (!original && !enter) return;
      if (original.length > 131072) { this.status.textContent = '内容过长，请使用文件上传'; return; }
      let data = original.replace(/\r\n|\n/g, '\r');
      if (enter && !data.endsWith('\r')) data += '\r';
      this.sending = true; this.execute.disabled = this.raw.disabled = true;
      this.status.textContent = '正在发送…';
      try {
        // Keep the captured server even when a user switches tabs mid-send.
        for (let offset = 0; offset < data.length;) {
          let end = Math.min(data.length, offset + 4096);
          if (end < data.length && /[\uD800-\uDBFF]/.test(data[end - 1])) end--;
          await this.send(id, data.slice(offset, end));
          offset = end;
        }
        if (this.drafts.get(id) === original) this.drafts.delete(id);
        if (id === this.sessionId) {
          if (this.input.value === original) this.input.value = '';
          this.status.textContent = enter ? '已发送并回车' : '已发送，未补回车';
          this.input.focus();
        }
      } catch {
        if (id === this.sessionId) this.status.textContent = '发送未确认，可能已发出部分内容；请先检查终端输出，不会自动重发';
      } finally {
        this.sending = false;
        this.execute.disabled = this.raw.disabled = !this.enabled;
      }
    }
  }
  window.SSHFallbackInput = SSHFallbackInput;
})();
