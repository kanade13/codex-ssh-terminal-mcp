// Both the browser page and the MCP App use the same ANSI terminal renderer.
(() => {
  'use strict';

  class SSHOutputTerminal {
    constructor(element, { onData, onResize, onNotice } = {}) {
      this.element = element;
      this.onData = onData;
      this.onResize = onResize;
      this.onNotice = onNotice || (() => {});
      this.enabled = false;
      this.frame = null;
      this.reset();
      this.observer = new ResizeObserver(() => this.scheduleFit());
      this.observer.observe(element);
      element.addEventListener('contextmenu', event => {
        if (event.shiftKey) return; // Shift+right-click keeps the browser menu.
        event.preventDefault();
        event.stopImmediatePropagation();
        if (this.enabled) this.pasteClipboard();
      }, true);
      element.addEventListener('mousedown', event => {
        if (event.button === 2 && !event.shiftKey) {
          event.preventDefault();
          event.stopImmediatePropagation();
        }
      }, true);
    }

    reset() {
      // Dispose the old write queue too, so a quick session switch cannot replay
      // buffered output from the previously selected server into the new view.
      this.terminal?.dispose();
      this.element.replaceChildren();
      this.terminal = new window.Terminal({
        cols: 80,
        rows: 24,
        scrollback: 10000,
        fontFamily: 'Consolas, "Liberation Mono", monospace',
        fontSize: 13,
        lineHeight: 1.25,
        cursorBlink: true,
        disableStdin: !this.enabled,
        // SSH emits CR/LF itself. Preserve CR redraws and cursor operations.
        convertEol: false,
        theme: {
          background: '#111418',
          foreground: '#e6edf3',
          cursor: '#e6edf3',
          selectionBackground: '#39546b',
          black: '#24292e',
          red: '#f97583',
          green: '#85e89d',
          yellow: '#ffea7f',
          blue: '#79b8ff',
          magenta: '#b392f0',
          cyan: '#76e3ea',
          white: '#d1d5da',
          brightBlack: '#6a737d',
          brightRed: '#fdaeb7',
          brightGreen: '#bef5cb',
          brightYellow: '#fff5b1',
          brightBlue: '#c8e1ff',
          brightMagenta: '#d1bcf9',
          brightCyan: '#b3f0ff',
          brightWhite: '#fafbfc',
        },
      });
      this.fitAddon = new window.FitAddon.FitAddon();
      this.terminal.loadAddon(this.fitAddon);
      this.terminal.open(this.element);
      this.terminal.onData(data => {
        if (this.enabled) this.onData?.(data);
      });
      this.terminal.onResize(size => this.onResize?.(size));
      this.terminal.attachCustomKeyEventHandler(event => {
        const key = event.key.toLowerCase();
        const modifier = event.ctrlKey || event.metaKey;
        // Handle only keydown; keyup must not trigger a duplicate paste.
        if (modifier && key === 'c' && (event.shiftKey || event.metaKey || this.terminal.hasSelection())) {
          if (event.type === 'keydown') { event.preventDefault(); this.copySelection(); }
          return false;
        }
        if (modifier && key === 'v' || event.shiftKey && key === 'insert') {
          // Standalone browsers can deliver their native paste event directly.
          if (!window.codexSSHNative) return true;
          if (event.type === 'keydown') { event.preventDefault(); this.pasteClipboard(); }
          return false;
        }
        if (event.ctrlKey && (key === '+' || key === '=' || key === '-' || key === '0')) {
          if (event.type === 'keydown') {
            event.preventDefault();
            const current = this.terminal.options.fontSize;
            this.terminal.options.fontSize = key === '0' ? 13 : Math.max(9, Math.min(28, current + (key === '-' ? -1 : 1)));
            this.scheduleFit();
          }
          return false;
        }
        return true;
      });
      this.scheduleFit();
    }

    scheduleFit() {
      if (this.frame !== null) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = null;
        if (this.element.clientWidth > 0 && this.element.clientHeight > 0) {
          this.fitAddon.fit();
        }
      });
    }

    write(text) {
      if (typeof text === 'string' && text) this.terminal.write(text);
    }

    async pasteClipboard() {
      if (!this.enabled || this.pasting) return;
      const target = this.terminal;
      this.pasting = true;
      try {
        let text;
        if (window.codexSSHNative?.readClipboard) text = await window.codexSSHNative.readClipboard();
        else text = await navigator.clipboard.readText();
        // A user may switch server tabs while clipboard access is pending.
        if (target !== this.terminal || !this.enabled) return;
        if (typeof text !== 'string' || !text) return;
        if (text.length > 524288) throw new Error('粘贴内容过大，请通过文件上传');
        target.focus();
        // xterm normalizes newlines and uses bracketed paste when the remote
        // application requests it. Never append Enter or emulate keystrokes.
        target.paste(text);
      } catch (error) { this.onNotice(error.message); }
      finally { this.pasting = false; }
    }

    async copySelection() {
      const text = this.terminal.getSelection();
      if (!text) return;
      try {
        if (window.codexSSHNative?.writeClipboard) await window.codexSSHNative.writeClipboard(text);
        else await navigator.clipboard.writeText(text);
      } catch (error) { this.onNotice(error.message); }
    }

    setEnabled(enabled) {
      this.enabled = Boolean(enabled);
      this.terminal.options.disableStdin = !this.enabled;
    }

    focus() { if (this.enabled) this.terminal.focus(); }

    size() { return { cols: this.terminal.cols, rows: this.terminal.rows }; }
  }

  window.SSHOutputTerminal = SSHOutputTerminal;
})();
