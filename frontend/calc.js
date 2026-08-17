// Экран «Расчёт» — пошаговый визард одной прокручиваемой страницей.
// Заполненные шаги сворачиваются в компактную строку, следующий раскрывается.
import { api } from './api.js?v=2';
import {
  h, frag, icon, switchToggle, fmtEur, fmtRub, fmtInt, toNum, toast,
  stateLoading, stateError, errBox, debounce, keepFocus,
} from './ui.js?v=2';
import { mainButton, haptic } from './tg.js?v=2';
import { photoGrid, galleryBar } from './gallery.js?v=2';

// Подпись у направления вместо декоративной картинки — она несёт смысл
export const DIRECTIONS = [
  { key: 'minsk', label: 'ЕС/Минск', sub: 'таможня РБ' },
  { key: 'kult40', label: 'ЕС/Культ40', sub: 'прямая таможня' },
  { key: 'msk', label: 'ЕС-МСК', sub: 'СВХ Москва' },
];

// Флаг здесь — настоящий признак страны, но дублируем его названием
const VATS = [
  { value: 1.19, label: '19%', ico: '🇩🇪', country: 'Германия' },
  { value: 1.17, label: '17%', ico: '🇧🇾', country: 'Беларусь' },
  { value: 1.21, label: '21%', ico: '🇧🇪', country: 'Бельгия' },
  { value: 1.0,  label: '0%',  ico: '',    country: 'без НДС' },
];

/** Коэффициент 1.19 → «19%». */
const vatLabel = (v) => `${Number(((Number(v) - 1) * 100).toFixed(2))}%`;

export const FIELD_LABELS = {
  customs_eur: 'Таможня РБ, €',
  util_rub: 'Утиль, ₽',
  customs_tks_rub: 'Таможня ТКС, ₽',
  evacuator_rub: 'Эвакуатор СПБ-МСК, ₽',
};

/** Запасной набор полей, если сервер не прислал fields. */
export const DIR_FIELDS = {
  minsk: ['customs_eur', 'util_rub'],
  kult40: ['evacuator_rub', 'customs_tks_rub'],
  msk: ['customs_tks_rub'],
};

export const dirLabel = (key) => (DIRECTIONS.find((d) => d.key === key) || {}).label || key || '—';

const PROGRESS_TEXTS = [
  'Открываю объявление…',
  'Читаю характеристики…',
  'Собираю фотографии…',
  'Ещё немного, страница тяжёлая…',
];

let st = null;
let mountRoot = null;
let calcSeq = 0;
let previewSeq = 0;
let progressTimer = null;

function freshState() {
  return {
    url: '',
    parsing: false, parseError: '', progressIdx: 0,
    draftId: null, car: null,
    direction: null,
    counterparty: '', counterpartyDone: false,
    vat: null,
    buyback: { mode: 'pct', value: 10 }, buybackChosen: false,
    customOpen: false, customValue: '',
    vatCustomOpen: false, vatCustomValue: '',
    fields: { customs_eur: '', util_rub: '', customs_tks_rub: '', evacuator_rub: '' },
    calc: null, calcLoading: false, calcError: '',
    photos: [],
    preview: null, previewLoading: false, previewError: '',
    open: 'link',
    details: false,          // раскрыты ли подробности расчёта
    confirming: false,       // показан ли экран подтверждения отправки
    withKp: true,            // «с КП» — фото и текст клиенту, «без КП» — только запись
    submitting: false, done: null,
  };
}

export function renderCalc(root) {
  if (!st) st = freshState();
  mountRoot = root;
  render();
}

function render() {
  if (!mountRoot || !mountRoot.isConnected) return;
  keepFocus(() => mountRoot.replaceChildren(build()));
  syncMainButton();
}

/* ---------- Сборка экрана ---------- */

function build() {
  if (st.done) return doneScreen();

  const parts = [stepLink()];
  if (st.car) parts.push(carCard(), stepDirection());
  if (st.direction) parts.push(stepCounterparty());
  if (st.counterpartyDone) parts.push(stepVat());
  if (st.vat) parts.push(stepBuyback());
  if (st.buybackChosen) {
    if (fieldList().length) parts.push(stepFields());
    parts.push(calcBlock());
    if (st.withKp) {
      parts.push(photosBlock());
      if (st.photos.length) parts.push(previewBlock());
    }
    if (st.confirming) parts.push(confirmBlock());
  }
  return frag(...parts);
}

/** Складной шаг: заголовок + сводка + тело. aside — элемент справа от заголовка. */
function step({ key, num, title, summary, done, body, aside }) {
  const open = st.open === key;
  const head = h('button', {
    class: 'step-head', type: 'button',
    'aria-expanded': open ? 'true' : 'false',
    onclick: () => { st.open = open ? null : key; render(); },
  },
    h('span', { class: 'step-num', text: String(num) }),
    h('span', { class: 'step-title', text: title }),
    !open && summary ? h('span', { class: 'step-summary', text: summary }) : null,
    h('span', { class: 'step-chev' },
      icon(open ? 'chevronDown' : (done ? 'pencil' : 'chevron'), 16)),
  );
  return h('section', { class: 'card step' + (open ? ' open' : '') + (done ? ' done' : '') },
    aside ? h('div', { class: 'step-row' }, head, aside) : head,
    open ? h('div', { class: 'step-body' }, body()) : null,
  );
}

/* --- 1. Ссылка --- */
function stepLink() {
  return step({
    key: 'link', num: 1, title: 'Ссылка', done: !!st.car,
    summary: st.car ? st.car.title : '',
    // Режим: с КП — фото и текст для клиента; без КП — только запись в таблицу
    aside: switchToggle({
      on: st.withKp,
      labelOn: 'с КП',
      labelOff: 'без КП',
      onChange: (on) => {
        st.withKp = on;
        if (!on) { st.preview = null; st.previewError = ''; }
        render();
        if (on && st.photos.length) schedulePreview();
      },
    }),
    body: () => {
      if (st.parsing) {
        return h('div', {},
          h('div', { class: 'row' }, h('span', { class: 'spinner' }), ' ',
            h('span', { text: PROGRESS_TEXTS[Math.min(st.progressIdx, PROGRESS_TEXTS.length - 1)] })),
          h('div', { class: 'progress' }, h('div', { class: 'progress-bar' })),
          h('div', { class: 'hint small mt8', text: 'Объявление может открываться до минуты — не закрывайте окно.' }),
        );
      }
      const input = h('input', {
        class: 'input', type: 'url', inputmode: 'url', placeholder: 'Вставьте ссылку на объявление',
        value: st.url, 'data-focus-key': 'url', autocomplete: 'off',
        oninput: (e) => { st.url = e.target.value; },
        onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); doParse(); } },
      });
      return h('div', {},
        h('div', { class: 'field' },
          input,
          h('div', { class: 'hint small mt8', text: 'Подходят mobile.de и autoscout24 — ссылка любого вида, целиком.' }),
        ),
        st.parseError ? h('div', { class: 'mb8' }, errBox(st.parseError)) : null,
        h('button', { class: 'btn btn-wide', type: 'button', text: 'Загрузить', onclick: doParse }),
      );
    },
  });
}

async function doParse() {
  const url = (st.url || '').trim();
  if (!url) { toast('Вставьте ссылку на объявление', 'error'); return; }

  st.parsing = true; st.parseError = ''; st.progressIdx = 0; st.open = 'link';
  render();
  startProgress();

  try {
    const r = await api.parse(url);
    st.draftId = r && r.draft_id;
    st.car = (r && r.car) || null;
    st.photos = [];
    st.preview = null;
    st.calc = null;
    st.open = 'direction';
    haptic.ok();
  } catch (e) {
    st.parseError = e.message;
    haptic.err();
  } finally {
    st.parsing = false;
    stopProgress();
    render();
  }
}

function startProgress() {
  stopProgress();
  progressTimer = setInterval(() => {
    if (!st.parsing) { stopProgress(); return; }
    if (st.progressIdx < PROGRESS_TEXTS.length - 1) { st.progressIdx += 1; render(); }
  }, 6000);
}
function stopProgress() {
  if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
}

/* --- 2. Карточка авто --- */
function carCard() {
  const c = st.car || {};
  const meta = [
    c.year,
    Number.isFinite(Number(c.mileage)) ? `${fmtInt(c.mileage)} км` : null,
    [c.engine_l ? `${c.engine_l} л` : null, c.power_hp ? `${c.power_hp} л.с.` : null].filter(Boolean).join(' ') || null,
    c.fuel,
  ].filter(Boolean).join(' · ');

  return h('section', { class: 'card' },
    (c.photos && c.photos.length) ? h('img', { class: 'car-photo', src: c.photos[0], alt: '', loading: 'lazy' }) : null,
    h('div', { class: 'card-pad' },
      h('h2', { class: 'car-title', text: c.title || 'Без названия' }),
      meta ? h('div', { class: 'car-meta', text: meta }) : null,
      h('div', { class: 'car-price', text: fmtEur(c.price_eur) }),
    ),
  );
}

/* --- 3. Направление --- */
function stepDirection() {
  return step({
    key: 'direction', num: 2, title: 'Направление', done: !!st.direction,
    summary: st.direction ? dirLabel(st.direction) : '',
    body: () => h('div', { class: 'chips' },
      ...DIRECTIONS.map((d) => h('button', {
        class: 'chip', type: 'button', 'aria-pressed': st.direction === d.key ? 'true' : 'false',
        onclick: () => {
          haptic.select();
          st.direction = d.key;
          st.open = 'counterparty';
          render();
          recalcNow();
        },
      },
        h('span', { class: 'chip-main', style: { fontSize: '14px' }, text: d.label }),
        h('span', { class: 'chip-sub', text: d.sub }),
      )),
    ),
  });
}

/* --- 4. Контрагент --- */
function stepCounterparty() {
  const submit = () => {
    if (!st.counterparty.trim()) { toast('Впишите контрагента', 'error'); return; }
    st.counterpartyDone = true;
    st.open = 'vat';
    render();
  };
  return step({
    key: 'counterparty', num: 3, title: 'Контрагент', done: st.counterpartyDone,
    summary: st.counterparty,
    body: () => h('div', {},
      h('div', { class: 'field' },
        h('input', {
          class: 'input', type: 'text', placeholder: 'Например: Иван',
          value: st.counterparty, 'data-focus-key': 'counterparty', autocomplete: 'off',
          oninput: (e) => { st.counterparty = e.target.value; },
          onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } },
        }),
        h('div', { class: 'hint small mt8', text: 'Ваше имя добавится к контрагенту автоматически.' })),
      h('button', { class: 'btn btn-wide', type: 'button', text: 'Дальше', onclick: submit }),
    ),
  });
}

/* --- 5. НДС --- */
function stepVat() {
  const cur = VATS.find((v) => v.value === st.vat);
  const isCustom = st.vat && !cur;

  const pick = (value) => {
    haptic.select();
    st.vat = value;
    st.vatCustomOpen = false;
    st.open = 'buyback';
    render();
    recalcNow();
  };

  const applyCustom = () => {
    const raw = String(st.vatCustomValue).replace('%', '').trim();
    const num = toNum(raw);
    if (num === null || num === undefined || num < 0 || num > 100) {
      toast('Введите процент от 0 до 100', 'error');
      return;
    }
    // Кто-то впишет коэффициент (1.19) — такого НДС не бывает
    const value = (num > 1 && num < 1.5 && /[.,]/.test(raw)) ? num : 1 + num / 100;
    pick(Number(value.toFixed(4)));
  };

  return step({
    key: 'vat', num: 4, title: 'НДС', done: !!st.vat,
    summary: cur ? `${cur.label} · ${cur.country}` : (isCustom ? vatLabel(st.vat) : ''),
    body: () => h('div', {},
      h('div', { class: 'chips' },
        ...VATS.map((v) => h('button', {
          class: 'chip', type: 'button', 'aria-pressed': st.vat === v.value ? 'true' : 'false',
          onclick: () => pick(v.value),
        },
          h('span', { class: 'chip-main', text: v.ico ? `${v.ico} ${v.label}` : v.label }),
          h('span', { class: 'chip-sub', text: v.country }),
        )),
        h('button', {
          class: 'chip chip-wide', type: 'button',
          'aria-pressed': (isCustom || st.vatCustomOpen) ? 'true' : 'false',
          onclick: () => {
            st.vatCustomOpen = !st.vatCustomOpen;
            if (st.vatCustomOpen && isCustom) st.vatCustomValue = String(Number(((st.vat - 1) * 100).toFixed(2)));
            render();
          },
        },
          h('span', { class: 'chip-main', text: isCustom ? vatLabel(st.vat) : 'Свой процент' }),
          h('span', { class: 'chip-sub', text: isCustom ? 'вручную' : 'ввести вручную' }),
        ),
      ),
      st.vatCustomOpen ? h('div', { class: 'row mt8' },
        h('input', {
          class: 'input num grow', type: 'text', inputmode: 'decimal', placeholder: 'Процент НДС, например 23',
          value: st.vatCustomValue, 'data-focus-key': 'custom-vat',
          oninput: (e) => { st.vatCustomValue = e.target.value; },
          onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); applyCustom(); } },
        }),
        h('button', { class: 'btn', type: 'button', text: 'Применить', onclick: applyCustom }),
      ) : null,
    ),
  });
}

/* --- 6. Выкуп --- */
function buybackSummary() {
  if (!st.buybackChosen) return '';
  const b = st.buyback;
  if (b.mode === 'pct') {
    const opt = (st.calc && st.calc.buyback_options || []).find((o) => Number(o.pct) === Number(b.value));
    return opt ? `${b.value}% — ${fmtEur(opt.eur)}` : `${b.value}%`;
  }
  return fmtEur(b.value);
}

function stepBuyback() {
  return step({
    key: 'buyback', num: 5, title: 'Выкуп', done: st.buybackChosen,
    summary: buybackSummary(),
    body: () => {
      if (st.calcError && !st.calc) return stateError(st.calcError, recalcNow);
      if (!st.calc) return stateLoading('Считаем варианты выкупа…');

      const opts = st.calc.buyback_options || [];
      const above = opts.filter((o) => !o.below_min);
      const hasBelow = opts.some((o) => o.below_min);
      const min = st.calc.buyback_min_eur;
      const b = st.buyback;
      const minActive = st.buybackChosen && b.mode === 'fixed' && Number(b.value) === Number(min);
      const customActive = st.buybackChosen && b.mode === 'fixed' && !minActive;

      const pick = (mode, value) => {
        haptic.select();
        st.buyback = { mode, value };
        st.buybackChosen = true;
        st.customOpen = false;
        st.open = fieldList().length ? 'fields' : null;
        render();
        recalcNow();
      };

      // Процент крупно, сумма подписью — читается по колонкам, а не построчно
      const buttons = above.map((o) => h('button', {
        class: 'chip', type: 'button',
        'aria-pressed': (st.buybackChosen && b.mode === 'pct' && Number(b.value) === Number(o.pct)) ? 'true' : 'false',
        onclick: () => pick('pct', o.pct),
      },
        h('span', { class: 'chip-main', text: `${o.pct}%` }),
        h('span', { class: 'chip-sub', text: fmtEur(o.eur) }),
      ));

      // Все варианты ниже минималки схлопываем в одну кнопку
      if (hasBelow && Number.isFinite(Number(min))) {
        buttons.unshift(h('button', {
          class: 'chip chip-wide', type: 'button', 'aria-pressed': minActive ? 'true' : 'false',
          onclick: () => pick('fixed', Number(min)),
        },
          h('span', { class: 'chip-main', text: fmtEur(min) }),
          h('span', { class: 'chip-sub', text: 'минималка' }),
        ));
      }

      buttons.push(h('button', {
        class: 'chip chip-wide', type: 'button',
        'aria-pressed': (customActive || st.customOpen) ? 'true' : 'false',
        onclick: () => {
          st.customOpen = !st.customOpen;
          if (st.customOpen && customActive) st.customValue = String(b.value);
          render();
        },
      },
        h('span', { class: 'chip-main', text: customActive ? fmtEur(b.value) : 'Своя сумма' }),
        h('span', { class: 'chip-sub', text: customActive ? 'вручную' : 'ввести вручную' }),
      ));

      const applyCustom = () => {
        const v = toNum(st.customValue);
        if (!v) { toast('Впишите сумму выкупа в евро', 'error'); return; }
        pick('fixed', v);
      };

      return h('div', {},
        h('div', { class: 'chips' }, ...buttons),
        st.customOpen ? h('div', { class: 'row mt8' },
          h('input', {
            class: 'input num grow', type: 'text', inputmode: 'decimal', placeholder: 'Сумма, €',
            value: st.customValue, 'data-focus-key': 'custom-buyback',
            oninput: (e) => { st.customValue = e.target.value; },
            onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); applyCustom(); } },
          }),
          h('button', { class: 'btn', type: 'button', text: 'Применить', onclick: applyCustom }),
        ) : null,
      );
    },
  });
}

/* --- 7. Поля направления --- */
function fieldList() {
  const fromCalc = st.calc && Array.isArray(st.calc.fields) ? st.calc.fields : null;
  return (fromCalc && fromCalc.length ? fromCalc : (DIR_FIELDS[st.direction] || []))
    .filter((k) => FIELD_LABELS[k]);
}

function stepFields() {
  const keys = fieldList();
  const summary = keys.map((k) => fmtInt(toNum(st.fields[k]))).join(' · ');
  return step({
    key: 'fields', num: 6, title: 'Таможня и расходы', done: true, summary,
    body: () => h('div', {},
      ...keys.map((k) => h('label', { class: 'field' },
        h('span', { class: 'field-label', text: FIELD_LABELS[k] }),
        h('input', {
          class: 'input num', type: 'text', inputmode: 'decimal', placeholder: '0',
          value: st.fields[k], 'data-focus-key': `field-${k}`,
          oninput: (e) => { st.fields[k] = e.target.value; scheduleRecalc(); },
        }),
      )),
    ),
  });
}

/* --- 8. Расчёт --- */

// Эмодзи уместны в чате, но не в интерфейсе: снимаем ведущий значок,
// а буква колонки Google-таблицы уходит в приглушённую подпись.
const LEADING_ICON_RE = /^[^\p{L}\p{N}(]+/u;

function splitLabel(label) {
  const clean = String(label || '').replace(LEADING_ICON_RE, '').trim();
  const m = clean.match(/^(.*?)\s*\(([A-Za-z]{1,2})\)$/);
  return m ? { text: m[1], col: m[2] } : { text: clean, col: '' };
}

function calcRow(r) {
  const { text, col } = splitLabel(r.label);
  const cls = r.role === 'sum' ? 'is-sum' : (r.role === 'key' ? 'is-key' : '');
  return h('tr', { class: cls },
    h('td', {},
      h('span', { text }),
      col ? h('span', { class: 'calc-col', text: col }) : null,
    ),
    h('td', { text: r.value }),
  );
}

function calcBlock() {
  const body = [];
  if (!st.calc && st.calcLoading) body.push(stateLoading('Считаем…'));
  else if (!st.calc && st.calcError) body.push(stateError(st.calcError, recalcNow));
  else if (st.calc) {
    // Итог показываем отдельным блоком, в таблице он не нужен
    const rows = (st.calc.rows || []).filter((r) => r.role !== 'total' && r.role !== 'sep');
    // Свёрнуто — только выбор менеджера и итоги блоков; остальное под «Подробностями»
    const visible = st.details ? rows : rows.filter((r) => r.role === 'sum' || r.role === 'key');

    const trs = [];
    let firstGroup = true;
    visible.forEach((r) => {
      if (st.details && r.group) {
        trs.push(h('tr', { class: 'calc-group' },
          h('td', { colspan: '2', class: firstGroup ? 'calc-group-first' : '', text: r.group })));
        firstGroup = false;
      }
      trs.push(calcRow(r));
    });

    body.push(h('table', { class: 'calc-table' }, h('tbody', {}, ...trs)));
    body.push(h('button', {
      class: 'disclosure', type: 'button', 'aria-expanded': st.details ? 'true' : 'false',
      onclick: () => { st.details = !st.details; render(); },
    },
      h('span', { class: 'disclosure-chev' }, icon('chevron', 14)),
      h('span', { text: st.details ? 'Скрыть подробности' : 'Подробности расчёта' }),
    ));
    body.push(h('div', { class: 'total' },
      h('span', { class: 'total-label', text: 'Под ключ' }),
      h('span', { class: 'total-value', text: fmtRub(st.calc.total_rub) })));
    if (st.calcError) body.push(h('div', { class: 'mt8' }, errBox(st.calcError)));
  }

  return h('section', { class: 'card card-pad' },
    h('div', { class: 'row mb8' },
      h('h2', { class: 'card-title grow', style: { margin: '0' }, text: 'Расчёт' }),
      st.calcLoading && st.calc ? h('span', { class: 'spinner' }) : null,
    ),
    ...body,
  );
}

/* --- 11. Подтверждение отправки --- */
function confirmBlock() {
  const c = st.car || {};
  const items = [
    ['Авто', c.title || '—'],
    ['Направление', dirLabel(st.direction)],
    ['Контрагент', st.counterparty.trim()],
    ['Выкуп', buybackSummary() || '—'],
    ...(st.withKp ? [['Фото', `${st.photos.length} шт.`]] : []),
    ['Под ключ', st.calc ? fmtRub(st.calc.total_rub) : '—'],
  ];
  return h('section', { class: 'card card-pad' },
    h('h2', { class: 'card-title', text: st.withKp ? 'Проверьте перед отправкой' : 'Проверьте перед записью' }),
    h('div', {
      class: 'hint',
      text: st.withKp
        ? 'Запись уйдёт в таблицу, КП — в чат. Отменить отправку будет нельзя.'
        : 'Запись уйдёт в таблицу. КП не формируется — отправить его можно позже из истории.',
    }),
    h('ul', { class: 'confirm-list' },
      ...items.map(([k, v]) => h('li', {},
        h('span', { class: 'k', text: k }),
        h('span', { class: 'v', text: String(v) }),
      )),
    ),
    h('button', {
      class: 'btn btn-second btn-wide mt8', type: 'button', text: 'Вернуться к правкам',
      onclick: () => { st.confirming = false; render(); },
    }),
  );
}

/* --- 9. Фото --- */
function photosBlock() {
  const all = (st.car && st.car.photos) || [];
  return h('section', { class: 'card card-pad' },
    h('h2', { class: 'card-title', text: 'Фото для КП' }),
    all.length === 0
      ? h('div', { class: 'hint', text: 'В объявлении не нашлось фотографий — КП уйдёт текстом.' })
      : frag(
        galleryBar({
          selectedCount: st.photos.length,
          total: all.length,
          onClear: () => { st.photos = []; st.preview = null; render(); },
        }),
        photoGrid({
          photos: all,
          selected: st.photos,
          onChange: (next) => { st.photos = next; render(); schedulePreview(); },
        }),
        h('div', { class: 'hint small mt8', text: 'Порядок важен: первое фото несёт текст КП.' }),
      ),
  );
}

/* --- 10. Предпросмотр --- */
function previewBlock() {
  const p = st.preview;
  // Комплектация не влезла в подпись к фото — уйдёт двумя сообщениями подряд
  const parts = p ? (Array.isArray(p.parts) && p.parts.length ? p.parts : [p.text || '']) : [];
  return h('section', { class: 'card card-pad' },
    h('h2', { class: 'card-title', text: 'Предпросмотр КП' }),
    st.previewLoading && !p ? stateLoading('Собираем текст…') : null,
    !st.previewLoading && st.previewError && !p ? stateError(st.previewError, () => loadPreview()) : null,
    p ? frag(
      ...parts.map((text, i) => frag(
        parts.length > 1
          ? h('div', { class: 'hint small', text: i === 0 ? 'Сообщение 1 — с фото' : 'Сообщение 2 — продолжение' })
          : null,
        h('pre', { class: 'kp-text', text }),
      )),
      h('div', { class: 'kp-counter', text: `${fmtInt(p.length)} / ${fmtInt(p.limit)} символов в подписи` }),
      parts.length > 1
        ? h('div', { class: 'hint small mt8', text: 'Комплектация целиком не влезает в подпись — бот пришлёт её двумя сообщениями.' })
        : null,
    ) : null,
  );
}

/* --- Готово --- */
function doneScreen() {
  const d = st.done || {};
  // КП не просили — отсутствие отправки это не ошибка
  const noKp = d.withKp === false;
  const failed = !noKp && d.sent === false;
  return h('section', { class: 'card card-pad center' },
    h('div', { class: 'done-mark' + (failed ? ' is-warn' : '') }, icon(failed ? 'alert' : 'check', 26)),
    h('h2', { class: 'car-title mt8', text: failed ? 'Записано, но КП не ушло' : 'Готово' }),
    failed
      ? h('div', { class: 'warn-box mt8', text: 'Запись сохранена, но КП не ушло в чат. Напишите боту /start и отправьте заново из истории.' })
      : h('div', { class: 'hint', text: noKp ? 'Записано в таблицу, без КП' : 'КП отправлен в чат' }),
    h('div', { class: 'car-price', text: `Запись #${d.car_num ?? '—'}` }),
    Number.isFinite(Number(d.sheet_row)) ? h('div', { class: 'hint small', text: `Строка в таблице: ${d.sheet_row}` }) : null,
    noKp ? h('div', { class: 'hint small mt8', text: 'КП можно отправить позже из истории.' }) : null,
  );
}

/* ---------- Запросы ---------- */

function calcBody() {
  return {
    draft_id: st.draftId,
    direction: st.direction,
    vat: st.vat,
    buyback: { mode: st.buyback.mode, value: Number(st.buyback.value) },
    customs_eur: toNum(st.fields.customs_eur),
    util_rub: toNum(st.fields.util_rub),
    customs_tks_rub: toNum(st.fields.customs_tks_rub),
    evacuator_rub: toNum(st.fields.evacuator_rub),
  };
}

const scheduleRecalc = debounce(() => recalcNow(), 400);

async function recalcNow() {
  if (!st.draftId || !st.direction || !st.vat) return;
  const seq = ++calcSeq;
  st.calcLoading = true;
  render();
  try {
    const r = await api.calc(calcBody());
    if (seq !== calcSeq) return;
    st.calc = r;
    st.calcError = '';
  } catch (e) {
    if (seq !== calcSeq) return;
    st.calcError = e.message;
  } finally {
    if (seq === calcSeq) {
      st.calcLoading = false;
      render();
      if (st.photos.length) schedulePreview();
    }
  }
}

const schedulePreview = debounce(() => loadPreview(), 400);

async function loadPreview() {
  if (!st.withKp) return;
  if (!st.draftId || !st.direction || !st.vat || !st.photos.length) return;
  const seq = ++previewSeq;
  st.previewLoading = true;
  render();
  try {
    const r = await api.preview({ ...calcBody(), photos: st.photos });
    if (seq !== previewSeq) return;
    st.preview = r;
    st.previewError = '';
  } catch (e) {
    if (seq !== previewSeq) return;
    st.previewError = e.message;
  } finally {
    if (seq === previewSeq) { st.previewLoading = false; render(); }
  }
}

/* ---------- Главная кнопка ---------- */

function isReady() {
  // Фото нужны только когда готовим КП
  const photosOk = !st.withKp || st.photos.length > 0;
  return !!(st.draftId && st.direction && st.vat && st.counterparty.trim() && st.buybackChosen && photosOk);
}

function syncMainButton() {
  if (st.done) {
    mainButton.set({ text: 'Новый расчёт', onClick: startNew });
    return;
  }
  if (!isReady()) { mainButton.hide(); return; }
  if (st.confirming) {
    mainButton.set({
      text: st.withKp ? 'Подтвердить и отправить' : 'Подтвердить и записать',
      onClick: doSubmit,
      enabled: !st.submitting,
      loading: st.submitting,
    });
    return;
  }
  // Действие необратимое — сначала показываем сводку на проверку
  mainButton.set({
    text: st.withKp ? 'Записать и отправить КП' : 'Записать в таблицу',
    onClick: () => {
      st.confirming = true;
      render();
      requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }));
    },
    enabled: true,
  });
}

async function doSubmit() {
  if (st.submitting || !isReady()) return;
  st.submitting = true;
  render();
  try {
    const r = await api.submit({
      ...calcBody(),
      counterparty: st.counterparty.trim(),
      photos: st.withKp ? st.photos : [],
      with_kp: st.withKp,
    });
    st.done = { ...(r || {}), withKp: st.withKp };
    st.confirming = false;
    haptic.ok();
  } catch (e) {
    toast(e.message, 'error');
    haptic.err();
  } finally {
    st.submitting = false;
    render();
  }
}

function startNew() {
  stopProgress();
  st = freshState();
  render();
}
