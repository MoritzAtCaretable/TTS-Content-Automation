/* Styled, select-only comboboxes. Native selects retain values and change events. */
(() => {
  'use strict';
  const widgets = new Map();
  let opened = null, serial = 0;
  const arrow = '<svg class="studio-select-arrow" viewBox="0 0 20 20" aria-hidden="true"><path d="m5 7.5 5 5 5-5"/></svg>';

  class SelectMenu {
    constructor(select) {
      this.select = select;
      this.id = `studio-select-${++serial}`;
      this.active = -1;
      this.buffer = '';
      this.host = select.closest('[data-select-field]');
      if (!this.host) {
        this.host = document.createElement('span');
        select.before(this.host);
        this.host.append(select);
      }
      this.host.classList.add('studio-select-host');
      this.button = document.createElement('button');
      this.button.type = 'button';
      this.button.className = 'studio-select-trigger';
      this.button.setAttribute('role', 'combobox');
      this.button.setAttribute('aria-haspopup', 'listbox');
      this.button.setAttribute('aria-expanded', 'false');
      this.button.setAttribute('aria-controls', `${this.id}-menu`);
      this.button.setAttribute('aria-labelledby', `${this.id}-label ${this.id}-value`);
      this.button.innerHTML = `<span class="studio-select-text"><span id="${this.id}-label"></span><span class="studio-select-value" id="${this.id}-value"></span></span>${arrow}`;
      const caption = this.button.querySelector(`#${this.id}-label`);
      caption.textContent = select.dataset.caption || select.getAttribute('aria-label') || 'Auswahl';
      caption.className = select.dataset.caption ? 'studio-select-caption' : 'studio-select-sr';
      this.value = this.button.querySelector('.studio-select-value');
      this.host.append(this.button);
      select.hidden = true;
      select.tabIndex = -1;
      this.button.onclick = () => opened === this ? this.close() : this.open();
      this.button.onkeydown = event => this.keydown(event);
      this.observer = new MutationObserver(() => this.sync());
      this.observer.observe(select, {childList: true, subtree: true, characterData: true, attributes: true,
        attributeFilter: ['disabled', 'selected', 'label', 'value', 'title']});
      select.addEventListener('change', () => this.sync());
      this.sync();
    }

    sync() {
      const text = this.select.selectedOptions[0]?.label || 'Bitte auswählen';
      if (this.value.textContent !== text) this.value.textContent = text;
      this.button.disabled = this.select.disabled || !this.select.options.length;
      this.button.title = this.select.title || text;
      if (opened === this) {
        const signature = Array.from(this.select.options, o => [o.label, o.value, o.disabled]);
        if (this.button.disabled || JSON.stringify(signature) !== this.signature) this.close();
      }
    }

    enabled() {
      return Array.from(this.select.options).map((option, index) => ({option, index}))
        .filter(({option}) => !option.disabled && !option.parentElement.disabled).map(({index}) => index);
    }

    open() {
      this.sync();
      if (this.button.disabled || !this.enabled().length) return;
      if (opened) opened.close();
      opened = this;
      this.buffer = '';
      this.menu = document.createElement('div');
      this.menu.id = `${this.id}-menu`;
      this.menu.className = 'studio-select-menu';
      this.menu.setAttribute('role', 'listbox');
      this.menu.setAttribute('aria-labelledby', `${this.id}-label`);
      this.signature = JSON.stringify(Array.from(this.select.options, o => [o.label, o.value, o.disabled]));
      for (const [index, option] of Array.from(this.select.options).entries()) {
        const row = document.createElement('div');
        row.className = 'studio-select-option';
        row.id = `${this.id}-option-${index}`;
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', String(index === this.select.selectedIndex));
        row.setAttribute('aria-disabled', String(!this.enabled().includes(index)));
        const label = document.createElement('span');
        label.textContent = option.label;
        const check = document.createElement('span');
        check.className = 'studio-select-check';
        check.textContent = index === this.select.selectedIndex ? '✓' : '';
        check.setAttribute('aria-hidden', 'true');
        row.append(label, check);
        row.onpointerdown = event => event.preventDefault(); // Keep focus on the combobox.
        row.onpointermove = () => this.highlight(index, false);
        row.onclick = () => this.choose(index);
        this.menu.append(row);
      }
      // Menus inside a modal must join its top layer; page menus avoid scroll clipping.
      (this.select.closest('dialog[open]') || document.body).append(this.menu);
      const bounds = this.button.getBoundingClientRect();
      const width = Math.min(Math.max(bounds.width, 220), window.innerWidth - 24);
      const below = window.innerHeight - bounds.bottom - 14, above = bounds.top - 14;
      const upward = below < 180 && above > below;
      this.menu.style.width = `${width}px`;
      this.menu.style.maxHeight = `${Math.max(60, Math.min(320, upward ? above : below))}px`;
      this.menu.style.left = `${Math.max(12, Math.min(bounds.left, window.innerWidth - width - 12))}px`;
      this.menu.style.top = `${upward ? Math.max(12, bounds.top - this.menu.offsetHeight - 6) : bounds.bottom + 6}px`;
      this.button.setAttribute('aria-expanded', 'true');
      this.button.focus({preventScroll: true});
      const selected = this.select.selectedIndex;
      this.highlight(this.enabled().includes(selected) ? selected : this.enabled()[0]);
    }

    highlight(index, scroll = true) {
      if (opened !== this || !this.enabled().includes(index)) return;
      this.active = index;
      Array.from(this.menu.children).forEach((row, i) => row.classList.toggle('active', i === index));
      const row = this.menu.children[index];
      this.button.setAttribute('aria-activedescendant', row.id);
      if (scroll) {
        // Scroll only the popup, never the page or the surrounding audio dialog.
        const item = row.getBoundingClientRect(), menu = this.menu.getBoundingClientRect();
        if (item.top < menu.top) this.menu.scrollTop -= menu.top - item.top;
        else if (item.bottom > menu.bottom) this.menu.scrollTop += item.bottom - menu.bottom;
      }
    }

    choose(index, focus = true) {
      if (this.select.disabled || !this.enabled().includes(index)) return;
      const changed = this.select.selectedIndex !== index;
      this.select.selectedIndex = index;
      this.close();
      this.sync();
      if (focus) this.button.focus({preventScroll: true});
      if (changed) {
        this.select.dispatchEvent(new Event('input', {bubbles: true}));
        this.select.dispatchEvent(new Event('change', {bubbles: true}));
      }
    }

    close() {
      this.menu?.remove();
      this.menu = null;
      this.button.setAttribute('aria-expanded', 'false');
      this.button.removeAttribute('aria-activedescendant');
      this.buffer = '';
      clearTimeout(this.timer);
      if (opened === this) opened = null;
    }

    keydown(event) {
      const isOpen = opened === this;
      if (event.key === 'Escape' && isOpen) {
        event.preventDefault(); event.stopPropagation(); this.close(); return;
      }
      if (event.key === 'Tab') {
        if (isOpen) this.choose(this.active, false);
        return;
      }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' '].includes(event.key)) {
        event.preventDefault();
        if (!isOpen) this.open();
        if (opened !== this) return;
        const enabled = this.enabled(), position = enabled.indexOf(this.active);
        if (event.key === 'Home') this.highlight(enabled[0]);
        else if (event.key === 'End') this.highlight(enabled[enabled.length - 1]);
        else if (isOpen && ['Enter', ' '].includes(event.key)) this.choose(this.active);
        else if (isOpen && event.key === 'ArrowDown') this.highlight(enabled[Math.min(position + 1, enabled.length - 1)]);
        else if (isOpen && event.key === 'ArrowUp') this.highlight(enabled[Math.max(position - 1, 0)]);
        return;
      }
      if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        if (!isOpen) this.open();
        if (opened !== this) return;
        const letter = event.key.toLocaleLowerCase();
        this.buffer = this.buffer === letter ? letter : this.buffer + letter;
        const enabled = this.enabled(), position = enabled.indexOf(this.active);
        const candidates = this.buffer.length > 1 ? enabled : [...enabled.slice(position + 1), ...enabled.slice(0, position + 1)];
        const match = candidates.find(i => this.select.options[i].label.toLocaleLowerCase().startsWith(this.buffer));
        if (match !== undefined) this.highlight(match);
        clearTimeout(this.timer);
        this.timer = setTimeout(() => { this.buffer = ''; }, 700);
      }
    }
  }

  function enhance(root = document) {
    for (const [select, widget] of widgets) {
      if (!select.isConnected) {
        widget.close(); widget.observer.disconnect(); widgets.delete(select);
      }
    }
    root.querySelectorAll('select').forEach(select => {
      if (!widgets.has(select)) widgets.set(select, new SelectMenu(select));
      else widgets.get(select).sync();
    });
  }
  window.StudioSelects = {sync: enhance, close: () => opened?.close()};
  document.addEventListener('pointerdown', event => {
    if (opened && !opened.host.contains(event.target) && !opened.menu.contains(event.target)) opened.close();
  }, true);
  document.addEventListener('focusin', event => {
    if (opened && event.target !== opened.button && !opened.menu.contains(event.target)) opened.close();
  });
  document.addEventListener('scroll', event => {
    if (opened && !opened.menu.contains(event.target)) opened.close();
  }, true);
  window.addEventListener('resize', () => opened?.close());
})();
