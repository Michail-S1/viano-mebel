'use strict';

// A local, persistent selection. Prices always come from the catalogue API.
(() => {
  if (window.CatalogKit) return;
  const STORAGE_KEY = 'viano.furniture-kit.v1';
  const MAX_QUANTITY = 999;
  const MAX_LINES = 500;
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const count = value => new Intl.NumberFormat('ru-RU').format(value);
  const currency = value => Number.isFinite(value) ? new Intl.NumberFormat('ru-RU', {maximumFractionDigits:2}).format(value) + ' ₽' : '—';
  const symbols = {
    kit:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M17.5 13v9M13 17.5h9"/>',
    plus:'<path d="M12 5v14M5 12h14"/>', minus:'<path d="M5 12h14"/>',
    close:'<path d="m6 6 12 12M18 6 6 18"/>',
    trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    refresh:'<path d="M20 10a8 8 0 1 0-1 8M20 3v7h-7"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>'
  };
  const svg = name => '<svg class="kit-icon" viewBox="0 0 24 24" aria-hidden="true">' + symbols[name] + '</svg>';
  let items = [], quote = null, cached = new Map(), open = false;
  let error = '', storageError = '', busy = false, revision = 0, requestTimer = null;
  let panel, launcher, peek, notice, quoteGeneration = 0, lastFocus = null;
  let undoItems = null, undoTimer = null, announcementTimer = null;
  const narrow = window.matchMedia('(max-width: 999px)');

  function sanitize(value) {
    if (!Array.isArray(value)) return [];
    const result = new Map();
    for (const item of value.slice(0, MAX_LINES)) {
      if (!item || typeof item.id !== 'string' || !item.id || item.id.length > 200) continue;
      const quantity = Number(item.quantity);
      if (!Number.isInteger(quantity) || quantity < 1 || quantity > MAX_QUANTITY) continue;
      result.set(item.id, Math.min(MAX_QUANTITY, (result.get(item.id) || 0) + quantity));
    }
    return Array.from(result, ([id, quantity]) => ({id, quantity}));
  }

  function readSelection() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      return saved?.version === 1 ? sanitize(saved.items) : [];
    } catch (_) { return []; }
  }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({version:1, items}));
      storageError = '';
    } catch (_) {
      storageError = 'Браузер не разрешил сохранить подборку. До закрытия этой страницы она останется доступна.';
    }
  }

  function signature() { return JSON.stringify(items); }
  function totalUnits() { return items.reduce((sum, item) => sum + item.quantity, 0); }
  function current(id) { return items.find(item => item.id === id); }
  function priceReady() { return !busy && !error && quote && quoteGeneration === revision; }
  function allPrices() { return priceReady() && quote.complete === true; }
  function productName(id) { return cached.get(id)?.product?.name || 'Товар недоступен'; }

  function button(id) {
    const selected = current(String(id));
    return '<button type="button" class="kit-add-button' + (selected ? ' is-selected' : '') + '" data-kit-add="' + escape(id) + '" aria-label="' + (selected ? 'Добавить ещё одну штуку в комплект' : 'Добавить в комплект') + '">' + svg(selected ? 'check' : 'plus') + '<span>' + (selected ? 'В комплекте · ' + count(selected.quantity) : 'В комплект') + '</span></button>';
  }

  function syncButtons() {
    document.querySelectorAll('[data-kit-add]').forEach(node => {
      const selected = current(node.dataset.kitAdd);
      node.classList.toggle('is-selected', Boolean(selected));
      node.setAttribute('aria-label', selected ? 'Добавить ещё одну штуку в комплект' : 'Добавить в комплект');
      node.innerHTML = svg(selected ? 'check' : 'plus') + '<span>' + (selected ? 'В комплекте · ' + count(selected.quantity) : 'В комплект') + '</span>';
    });
  }

  function announce(text, action) {
    if (!notice) return;
    clearTimeout(announcementTimer);
    notice.innerHTML = '<span>' + escape(text) + '</span>' + (action === 'undo' ? '<button type="button" data-kit-action="undo">Отменить</button>' : action === 'open' ? '<button type="button" data-kit-action="open">Открыть</button>' : '') + '<button type="button" class="kit-notice-close" data-kit-action="dismiss" aria-label="Закрыть сообщение">' + svg('close') + '</button>';
    notice.hidden = false;
    announcementTimer = setTimeout(() => { notice.hidden = true; }, action === 'undo' ? 15000 : 4500);
  }

  function updateChrome() {
    if (!launcher) return;
    launcher.setAttribute('aria-expanded', String(open));
    launcher.querySelector('.kit-launch-count').textContent = count(totalUnits());
    launcher.querySelector('.kit-launch-count').hidden = !items.length;
    launcher.setAttribute('aria-label', 'Подборка комплекта' + (items.length ? ', товаров: ' + count(totalUnits()) : ''));
    peek.hidden = open || !items.length;
    peek.innerHTML = svg('kit') + '<span><strong>Мой комплект <b>' + count(totalUnits()) + '</b></strong><small>' + (priceReady() ? (allPrices() ? '' : 'Частично · ') + currency(quote.total_sale) : busy ? 'Пересчитываем…' : 'Проверить цены') + '</small></span>' + svg('arrow');
    document.body.classList.toggle('catalog-kit-open', open);
  }

  function line(item, index) {
    const entry = cached.get(item.id), product = entry?.product;
    const valid = priceReady(), present = valid && product;
    const available = present && Number.isFinite(entry.sale_total) && Number.isFinite(entry.base_total);
    const name = product?.name || (valid ? 'Товар больше не найден' : error ? 'Товар не загружен' : 'Загружаем товар…');
    const meta = product ? [product.factory, product.dimensions, product.color].filter(Boolean).join(' · ') : 'Позиция сохранена в подборке';
    const photo = product?.photo_url && /^\/(?!\/)/.test(product.photo_url) ? '<img src="' + escape(product.photo_url) + '" alt="" loading="lazy">' : svg('kit');
    return '<article class="kit-line' + (valid && !available ? ' kit-line-unavailable' : '') + '" data-kit-line="' + escape(item.id) + '">' +
      '<div class="kit-line-top"><span class="kit-line-number">' + (index + 1) + '</span><div class="kit-line-photo">' + photo + '</div><div class="kit-line-info"><h3>' + escape(name) + '</h3><p>' + escape(meta) + '</p></div><button type="button" class="kit-icon-button kit-remove" data-kit-remove="' + escape(item.id) + '" data-kit-focus="remove-' + escape(item.id) + '" aria-label="Убрать ' + escape(name) + ' из комплекта">' + svg('close') + '</button></div>' +
      '<div class="kit-line-bottom"><div class="kit-quantity" role="group" aria-label="Количество ' + escape(name) + '"><button type="button" data-kit-step="-1" data-kit-id="' + escape(item.id) + '" data-kit-focus="minus-' + escape(item.id) + '" aria-label="Уменьшить количество"' + (item.quantity <= 1 ? ' disabled' : '') + '>' + svg('minus') + '</button><input type="number" min="1" max="999" step="1" inputmode="numeric" value="' + item.quantity + '" data-kit-quantity="' + escape(item.id) + '" data-kit-focus="quantity-' + escape(item.id) + '" aria-label="Количество ' + escape(name) + '"><button type="button" data-kit-step="1" data-kit-id="' + escape(item.id) + '" data-kit-focus="plus-' + escape(item.id) + '" aria-label="Увеличить количество"' + (item.quantity >= MAX_QUANTITY ? ' disabled' : '') + '>' + svg('plus') + '</button></div>' +
      '<div class="kit-line-prices">' + (available ? '<strong>' + currency(entry.sale_total) + '</strong><span>' + item.quantity + ' × ' + currency(product.sale_price) + '</span><small>Из прайса: ' + currency(entry.base_total) + '</small>' : '<strong class="kit-price-unavailable">' + (busy ? 'Проверяем цену…' : error ? 'Цена не проверена' : !product ? 'Товар недоступен' : 'Цена не указана') + '</strong>' + (valid ? '<small>Не включён в итог</small>' : '')) + '</div></div></article>';
  }

  function render() {
    if (!panel) return;
    const active = panel.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = active?.dataset.kitFocus;
    const typedValue = active?.matches('[data-kit-quantity]') ? active.value : null;
    const scrollTop = panel.querySelector('.kit-lines')?.scrollTop || 0;
    const valid = priceReady(), partial = valid && !allPrices();
    panel.innerHTML = '<header class="kit-panel-header"><div><p class="kit-eyebrow">СОБИРАЕМ ВМЕСТЕ</p><h2 id="kitPanelTitle">Подборка комплекта<span class="kit-title-dot">.</span></h2></div><button type="button" class="kit-icon-button kit-panel-close" data-kit-action="close" aria-label="Закрыть подборку">' + svg('close') + '</button></header>' +
      '<div class="kit-panel-caption"><span>' + (items.length ? count(items.length) + ' поз. · ' + count(totalUnits()) + ' шт.' : 'Мебель для вашего проекта') + '</span><button type="button" data-kit-action="clear" class="kit-text-button"' + (!items.length ? ' disabled' : '') + '>' + svg('trash') + 'Очистить</button></div>' +
      (storageError ? '<p class="kit-alert" role="status">' + escape(storageError) + '</p>' : '') +
      (error ? '<div class="kit-alert kit-error" role="alert"><span>' + escape(error) + '</span><button type="button" data-kit-action="refresh">Повторить</button></div>' : '') +
      (partial ? '<div class="kit-alert kit-partial" role="status">Итог неполный: ' + (Array.isArray(quote.missing) && quote.missing.length ? 'не найдены позиции — ' + count(quote.missing.length) + '. ' : 'для части позиций нет подтверждённой цены. ') + 'Проверьте отмеченные строки.</div>' : '') +
      '<div class="kit-lines"' + (busy ? ' aria-busy="true"' : '') + '>' + (items.length ? items.map(line).join('') : '<div class="kit-empty"><div class="kit-empty-art">' + svg('kit') + '</div><h3>Начнём с первой детали</h3><p>Найдите мебель в каталоге и нажмите «В комплект». Здесь можно менять количество и сразу видеть общую стоимость.</p><button type="button" class="kit-search-button" data-kit-action="search">' + svg('plus') + 'Выбрать мебель</button><span>Подборка сохранится в этом браузере</span></div>') + '</div>' +
      '<footer class="kit-panel-footer"><div class="kit-total-label"><span>' + (partial ? 'Подтверждённая часть комплекта' : 'Стоимость комплекта') + '</span><button type="button" class="kit-icon-button kit-refresh" data-kit-action="refresh" aria-label="Проверить актуальные цены" title="Проверить актуальные цены"' + (!items.length || busy ? ' disabled' : '') + '>' + svg('refresh') + '</button></div><strong class="kit-total-price" aria-live="polite" aria-atomic="true">' + (!items.length ? currency(0) : busy ? '<span class="kit-total-pending">Пересчитываем…</span>' : valid ? currency(quote.total_sale) : '—') + '</strong><p class="kit-total-base">Цена из прайса: ' + (!items.length ? currency(0) : valid ? currency(quote.total_base) : '—') + '</p><p class="kit-footer-hint">' + (items.length ? 'Подборка сохранена в этом браузере. Цены — из текущего каталога.' : 'Добавляйте товары из разных фабрик и коллекций.') + '</p></footer>';
    panel.querySelector('.kit-lines').scrollTop = scrollTop;
    if (focusKey) {
      const replacement = Array.from(panel.querySelectorAll('[data-kit-focus]')).find(node => node.dataset.kitFocus === focusKey);
      if (replacement && !replacement.disabled) {
        replacement.focus({preventScroll:true});
        if (typedValue !== null) replacement.value = typedValue;
      }
    }
    updateChrome();
  }

  function setOpen(value, focus) {
    open = Boolean(value);
    panel.hidden = !open;
    updateChrome();
    if (open) {
      if (focus) { lastFocus = document.activeElement; panel.querySelector('[data-kit-action="close"]').focus({preventScroll:true}); }
      refresh();
    } else if (focus && lastFocus?.isConnected) lastFocus.focus({preventScroll:true});
  }

  function changed() {
    revision += 1;
    quote = null; error = ''; busy = items.length > 0;
    save(); syncButtons(); render();
    clearTimeout(requestTimer);
    if (items.length) requestTimer = setTimeout(requestQuote, 110);
  }

  async function requestQuote() {
    const requestedRevision = revision, requestedSignature = signature();
    requestTimer = null;
    if (!items.length) { busy = false; quote = null; render(); return; }
    busy = true; error = ''; render();
    try {
      const result = await api('/api/selection/quote', {items:items.map(item => ({id:item.id, quantity:item.quantity}))});
      if (requestedRevision !== revision || requestedSignature !== signature()) return;
      if (!result || !Array.isArray(result.items) || !Number.isFinite(result.total_base) || !Number.isFinite(result.total_sale)) throw new Error('Не удалось получить корректный расчёт. Попробуйте обновить цены.');
      const received = new Map(result.items.map(item => [item.id, item]));
      if (items.some(item => !received.has(item.id) || received.get(item.id).quantity !== item.quantity)) throw new Error('Расчёт получен не для всей подборки. Обновите цены.');
      const redirected = items.some(item => received.get(item.id)?.product?.id && received.get(item.id).product.id !== item.id);
      if (redirected) {
        const mapped=items.map(item=>({id:received.get(item.id)?.product?.id || item.id,quantity:item.quantity}));
        const totals=new Map();mapped.forEach(item=>totals.set(item.id,(totals.get(item.id)||0)+item.quantity));
        if ([...totals.values()].some(value=>value>MAX_QUANTITY)) throw new Error('После объединения дублей количество превышает 999. Уменьшите количество в строках комплекта.');
        items=sanitize(mapped);changed();return;
      }
      quote = result; quoteGeneration = requestedRevision;
      cached = received;
    } catch (problem) {
      if (requestedRevision !== revision || requestedSignature !== signature()) return;
      quote = null; error = problem.message || 'Не удалось обновить цены. Повторите расчёт.';
    } finally {
      if (requestedRevision === revision && requestedSignature === signature()) { busy = false; render(); }
    }
  }

  function refresh() {
    clearTimeout(requestTimer);
    if (!items.length) { busy = false; quote = null; render(); return; }
    revision += 1;
    busy = true; error = ''; render();
    requestTimer = setTimeout(requestQuote, 80);
  }

  function add(id) {
    if (!id || typeof id !== 'string') return;
    const selected = current(id);
    if (selected?.quantity >= MAX_QUANTITY) { announce('Для одной позиции можно указать до 999 штук.'); return; }
    if (!selected && items.length >= MAX_LINES) { announce('В одном комплекте можно собрать до 500 позиций.'); return; }
    if (selected) selected.quantity += 1; else items.push({id, quantity:1});
    changed();
    if (!narrow.matches && !open) setOpen(true, false);
    announce(selected ? 'Количество в комплекте увеличено.' : 'Товар добавлен в комплект.', narrow.matches && !open ? 'open' : null);
  }

  function setQuantity(id, value) {
    const selected = current(id), quantity = Number(value);
    if (!selected) return;
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > MAX_QUANTITY) {
      announce('Укажите целое количество от 1 до 999.');
      render(); return;
    }
    if (selected.quantity !== quantity) { selected.quantity = quantity; changed(); }
  }

  function clear() {
    if (!items.length) return;
    clearTimeout(undoTimer);
    undoItems = items.map(item => ({...item}));
    items = []; changed();
    announce('Подборка очищена.', 'undo');
    undoTimer = setTimeout(() => { undoItems = null; }, 15000);
  }

  function undo() {
    if (!undoItems) return;
    const merged = new Map(undoItems.map(item => [item.id, item.quantity]));
    items.forEach(item => merged.set(item.id, Math.min(MAX_QUANTITY, (merged.get(item.id) || 0) + item.quantity)));
    items = sanitize(Array.from(merged, ([id, quantity]) => ({id, quantity})));
    undoItems = null; clearTimeout(undoTimer); changed();
    announce('Подборка восстановлена.');
  }

  function initialize() {
    items = readSelection();
    panel = document.createElement('aside');
    panel.id = 'catalogKitPanel'; panel.className = 'kit-panel'; panel.hidden = true;
    panel.setAttribute('aria-labelledby', 'kitPanelTitle');
    document.body.append(panel);
    launcher = document.createElement('button');
    launcher.type = 'button'; launcher.className = 'kit-launch';
    launcher.dataset.kitAction = 'toggle'; launcher.setAttribute('aria-controls', panel.id);
    launcher.innerHTML = svg('kit') + '<span>Подборка комплекта</span><b class="kit-launch-count" hidden>0</b>';
    const header = document.querySelector('.page-header');
    if (header) {
      let actions = header.querySelector('.catalog-header-actions');
      if (!actions) {
        actions = document.createElement('div'); actions.className = 'catalog-header-actions';
        const imports = header.querySelector('.header-import');
        if (imports) { imports.before(actions); actions.append(imports); } else header.append(actions);
      }
      actions.prepend(launcher);
    } else { launcher.classList.add('kit-launch-fallback'); document.body.append(launcher); }
    peek = document.createElement('button'); peek.type = 'button'; peek.className = 'kit-peek';
    peek.dataset.kitAction = 'open'; peek.setAttribute('aria-controls', panel.id); document.body.append(peek);
    notice = document.createElement('div'); notice.className = 'kit-notice'; notice.setAttribute('role','status');
    notice.setAttribute('aria-live','polite'); notice.hidden = true; document.body.append(notice);
    render(); refresh();

    // Capture before catalogue row/card listeners so adding never opens a product.
    document.addEventListener('click', event => {
      if (!(event.target instanceof Element)) return;
      const target = event.target.closest('[data-kit-add],[data-kit-action],[data-kit-remove],[data-kit-step]');
      if (!target || target.disabled) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (target.hasAttribute('data-kit-add')) { add(target.dataset.kitAdd); return; }
      if (target.hasAttribute('data-kit-remove')) {
        const name = productName(target.dataset.kitRemove);
        items = items.filter(item => item.id !== target.dataset.kitRemove); changed();
        announce('Убрано из комплекта: ' + name); return;
      }
      if (target.hasAttribute('data-kit-step')) {
        const selected = current(target.dataset.kitId);
        if (selected) setQuantity(selected.id, selected.quantity + Number(target.dataset.kitStep));
        return;
      }
      switch (target.dataset.kitAction) {
        case 'toggle': setOpen(!open, true); break;
        case 'open': setOpen(true, true); break;
        case 'close': setOpen(false, true); break;
        case 'clear': clear(); break;
        case 'undo': undo(); break;
        case 'refresh': refresh(); break;
        case 'dismiss': notice.hidden = true; break;
        case 'search': setOpen(false, false); document.getElementById('searchInput')?.focus(); break;
      }
    }, true);
    panel.addEventListener('change', event => {
      if (event.target.matches('[data-kit-quantity]')) {
        const id = event.target.dataset.kitQuantity, selected = current(id);
        const value = event.target.value;
        if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > MAX_QUANTITY) {
          if (selected) event.target.value = selected.quantity;
          announce('Укажите целое количество от 1 до 999.'); return;
        }
        setQuantity(id, value);
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && open && !document.querySelector('dialog[open]')) { event.preventDefault(); setOpen(false, true); }
      if (event.target instanceof Element && event.target.matches('[data-kit-quantity]') && event.key === 'Enter') { event.preventDefault(); event.target.blur(); }
    });
    window.addEventListener('storage', event => {
      if (event.key !== STORAGE_KEY && event.key !== null) return;
      items = readSelection(); revision += 1; quote = null;
      clearTimeout(undoTimer); undoItems = null;
      syncButtons(); refresh();
    });
    window.addEventListener('focus', () => { if (items.length) refresh(); });
    // Catalogue renderers may call button() after this module has initialized.
    const catalogue = document.getElementById('catalogBody');
    if (catalogue) new MutationObserver(() => {
      document.querySelectorAll('[data-kit-add]').forEach(node => {
        const selected = current(node.dataset.kitAdd);
        node.classList.toggle('is-selected', Boolean(selected));
      });
    }).observe(catalogue, {childList:true});
  }

  window.CatalogKit = Object.freeze({button, refresh, open:() => setOpen(true, true), close:() => setOpen(false, false)});
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once:true});
  else initialize();
})();
