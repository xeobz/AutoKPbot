// Мелкие DOM-хелперы, тосты, форматирование чисел, типовые состояния экранов.

/** Создать элемент. props: class, text, html, style, dataset, onclick и любые атрибуты. */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'html') el.innerHTML = v; // только для своей разметки, не для данных с сервера
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    // value у полей ввода — свойство, а не атрибут: у <textarea> атрибута
    // value нет вовсе, и поле рисовалось пустым, сколько в него ни пиши
    else if (k === 'value' && (tag === 'input' || tag === 'textarea' || tag === 'select')) el.value = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, kids) {
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    if (Array.isArray(kid)) append(el, kid);
    else if (kid instanceof Node) el.appendChild(kid);
    else el.appendChild(document.createTextNode(String(kid)));
  }
}

export const frag = (...children) => {
  const f = document.createDocumentFragment();
  append(f, children);
  return f;
};

/* ---------- Иконки ----------
   Эмодзи в интерфейсе выглядят по-разному на каждой платформе и не красятся,
   поэтому функциональные значки — простые SVG в цвете текста. */
const ICON_PATHS = {
  pencil: '<path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  chevronDown: '<path d="M6 9l6 6 6-6"/>',
  check: '<path d="M4 12.5l5 5L20 6.5"/>',
  alert: '<path d="M12 8v5M12 17h.01"/><path d="M10.3 3.9 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  sticker: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v6L13 20H6.5A2.5 2.5 0 0 1 4 17.5z"/><path d="M20 12.5h-4.5a2 2 0 0 0-2 2V20"/>',
};

/**
 * Переключатель с подписью. Клик не всплывает наверх — иначе тумблер
 * внутри заголовка шага сворачивал бы сам шаг.
 */
export function switchToggle({ on, labelOn, labelOff, onChange }) {
  return h('button', {
    class: 'switch' + (on ? ' is-on' : ''),
    type: 'button',
    role: 'switch',
    'aria-checked': on ? 'true' : 'false',
    onclick: (e) => { e.stopPropagation(); onChange(!on); },
  },
    h('span', { class: 'switch-text', text: on ? labelOn : labelOff }),
    h('span', { class: 'switch-track' }, h('span', { class: 'switch-knob' })),
  );
}

/** Инлайн-SVG иконка в цвете текста. size — сторона в px. */
export function icon(name, size = 18, cls = '') {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  el.setAttribute('viewBox', '0 0 24 24');
  el.setAttribute('fill', 'none');
  el.setAttribute('stroke', 'currentColor');
  el.setAttribute('stroke-width', '1.7');
  el.setAttribute('stroke-linecap', 'round');
  el.setAttribute('stroke-linejoin', 'round');
  el.setAttribute('width', String(size));
  el.setAttribute('height', String(size));
  el.setAttribute('aria-hidden', 'true');
  if (cls) el.setAttribute('class', cls);
  el.style.flex = 'none';
  el.style.display = 'block';
  el.innerHTML = ICON_PATHS[name] || '';
  return el;
}

/* ---------- Числа ---------- */
const NF = new Intl.NumberFormat('ru-RU');

export function fmtInt(n) {
  const v = Number(n);
  return Number.isFinite(v) ? NF.format(Math.round(v)) : '—';
}
export const fmtEur = (n) => (Number.isFinite(Number(n)) ? fmtInt(n) + ' €' : '—');
export const fmtRub = (n) => (Number.isFinite(Number(n)) ? fmtInt(n) + ' ₽' : '—');

/** Строка из поля ввода → число (терпит пробелы и запятую). */
export function toNum(v) {
  if (v === null || v === undefined || v === '') return 0;
  const s = String(v).replace(/[\s  ]/g, '').replace(',', '.');
  const n = Number(s);
  return Number.isFinite(n) ? n : 0;
}

/* ---------- Тосты (вместо alert) ---------- */
export function toast(message, type = 'info', ms = 4000) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const t = h('div', { class: 'toast' + (type ? ' ' + type : ''), text: String(message || '') });
  const kill = () => { t.remove(); };
  t.addEventListener('click', kill);
  box.appendChild(t);
  setTimeout(kill, ms);
}

/* ---------- Типовые состояния ---------- */
export function stateLoading(text = 'Загружаем…') {
  return h('div', { class: 'state' }, h('span', { class: 'spinner' }), ' ', h('span', { text }));
}

export function stateEmpty(title, sub) {
  return h('div', { class: 'state' },
    h('div', { class: 'state-title', text: title }),
    sub ? h('div', { text: sub }) : null);
}

export function stateError(text, onRetry) {
  return h('div', { class: 'state' },
    h('div', { class: 'err-box', text: text || 'Что-то пошло не так' }),
    onRetry ? h('button', { class: 'btn btn-second btn-sm mt8', type: 'button', text: 'Повторить', onclick: onRetry }) : null);
}

export function errBox(text) {
  return h('div', { class: 'err-box', text });
}

/* ---------- Прочее ---------- */
export function debounce(fn, ms) {
  let id = null;
  const wrapped = (...args) => {
    clearTimeout(id);
    id = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(id);
  return wrapped;
}

/**
 * Перерисовка целиком ломает фокус в полях ввода — запоминаем и возвращаем.
 * Работает для элементов с data-focus-key.
 */
export function keepFocus(render) {
  const active = document.activeElement;
  const key = active && active.dataset ? active.dataset.focusKey : null;
  let sel = null;
  if (key) { try { sel = [active.selectionStart, active.selectionEnd]; } catch { sel = null; } }
  render();
  if (!key) return;
  const next = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
  if (!next) return;
  next.focus({ preventScroll: true });
  if (sel && sel[0] !== null) { try { next.setSelectionRange(sel[0], sel[1]); } catch { /* не текстовое поле */ } }
}
