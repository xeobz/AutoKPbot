// Экран «История»: список записей → карточка с расчётом, правкой полей и переотправкой КП.
import { api } from './api.js?v=2';
import {
  h, frag, fmtRub, toNum, toast,
  stateLoading, stateEmpty, stateError, errBox, keepFocus,
} from './ui.js?v=2';
import { mainButton, haptic } from './tg.js?v=2';
import { FIELD_LABELS, DIR_FIELDS, dirLabel } from './calc.js?v=2';

let st = {
  list: null, listLoading: false, listError: '',
  openId: null,
  detail: null, detailLoading: false, detailError: '',
  edits: {}, saving: false, sending: false,
};
let mountRoot = null;

export function renderHistory(root) {
  mountRoot = root;
  // Возврат на вкладку показывает список — но не выкидываем из карточки, если есть несохранённые правки
  if (st.openId && !Object.keys(st.edits).length) { st.openId = null; st.detail = null; }
  render();
  // Список перечитываем при каждом заходе: пока пользователь считал в соседней
  // вкладке, там появилась новая запись — иначе история выглядит пустой
  if (!st.listLoading) loadList();
}

function render() {
  if (!mountRoot || !mountRoot.isConnected) return;
  keepFocus(() => mountRoot.replaceChildren(st.openId ? detailScreen() : listScreen()));
  syncMainButton();
}

/* ---------- Список ---------- */

async function loadList() {
  st.listLoading = true; st.listError = '';
  render();
  try {
    const r = await api.history(20);
    st.list = Array.isArray(r) ? r : [];
  } catch (e) {
    st.listError = e.message;
  } finally {
    st.listLoading = false;
    render();
  }
}

function listScreen() {
  if (st.listLoading && !st.list) return stateLoading('Загружаем историю…');
  if (st.listError && !st.list) return stateError(st.listError, loadList);
  if (!st.list || !st.list.length) {
    return stateEmpty('Пока пусто', 'Сделайте первый расчёт — записи появятся здесь.');
  }

  return frag(
    h('div', { class: 'row mb8' },
      h('span', { class: 'card-title grow', style: { margin: '0' }, text: 'История' }),
      h('button', { class: 'btn-ghost', type: 'button', text: 'Обновить', onclick: loadList }),
    ),
    h('section', { class: 'card' },
      ...st.list.map((it) => h('button', {
        class: 'list-item', type: 'button',
        onclick: () => { haptic.select(); openDetail(it.id); },
      },
        h('div', { class: 'list-item-top' },
          h('span', { class: 'list-item-name', text: it.car_name || 'Без названия' }),
          h('span', { class: 'list-item-num', text: it.car_num ? `#${it.car_num}` : '' }),
        ),
        h('div', { class: 'list-item-sub' },
          h('span', { text: [it.counterparty, dirLabel(it.direction)].filter(Boolean).join(' · ') }),
          h('span', { class: 'nums', text: it.created_at || '' }),
        ),
      )),
    ),
  );
}

/* ---------- Карточка ---------- */

function openDetail(id) {
  st.openId = id;
  st.detail = null;
  st.detailError = '';
  st.edits = {};
  render();
  loadDetail();
}

async function loadDetail() {
  st.detailLoading = true; st.detailError = '';
  render();
  try {
    st.detail = await api.historyItem(st.openId);
  } catch (e) {
    st.detailError = e.message;
  } finally {
    st.detailLoading = false;
    render();
  }
}

function backToList() {
  st.openId = null; st.detail = null; st.edits = {};
  render();
}

/** Выкуп может прийти объектом или числом — приводим к {mode, value}. */
function normBuyback(v) {
  if (v && typeof v === 'object') {
    return { mode: v.mode === 'fixed' ? 'fixed' : 'pct', value: Number(v.value) || 0 };
  }
  const n = Number(v);
  if (Number.isFinite(n) && n > 0) return { mode: n <= 100 ? 'pct' : 'fixed', value: n };
  return { mode: 'pct', value: 0 };
}

function detailScreen() {
  const back = h('button', { class: 'btn-ghost mb8', type: 'button', text: '‹ К списку', onclick: backToList });

  if (st.detailLoading && !st.detail) return frag(back, stateLoading('Открываем запись…'));
  if (st.detailError && !st.detail) return frag(back, stateError(st.detailError, loadDetail));
  if (!st.detail) return frag(back, stateEmpty('Запись не найдена'));

  const d = st.detail;
  const data = d.data || {};
  const rows = d.rows || [];
  const dirKeys = (Array.isArray(data.fields) && data.fields.length ? data.fields : (DIR_FIELDS[data.direction] || []))
    .filter((k) => FIELD_LABELS[k]);
  // У Культ40 и МСК утиль выбирается кнопкой, а не полем, но исправить сумму
  // в готовой записи всё равно нужно
  if ((data.direction === 'kult40' || data.direction === 'msk') && !dirKeys.includes('util_rub')) {
    dirKeys.push('util_rub');
  }
  // Курс правится у любого направления: он меняется по нескольку раз в день
  if (!dirKeys.includes('rate_eur_usdt')) dirKeys.push('rate_eur_usdt');
  if (!dirKeys.includes('rate_usdt_rub')) dirKeys.push('rate_usdt_rub');
  const bb = normBuyback(st.edits.buyback !== undefined ? st.edits.buyback : data.buyback);

  const val = (key, fallback) => (st.edits[key] !== undefined ? st.edits[key] : (fallback ?? ''));
  const setEdit = (key, v) => { st.edits[key] = v; };

  const header = h('section', { class: 'card card-pad' },
    h('h2', { class: 'car-title', text: data.car_name || data.title || `Запись #${data.car_num ?? d.id}` }),
    h('div', { class: 'car-meta', text: [data.car_num ? `#${data.car_num}` : null, dirLabel(data.direction), data.created_at].filter(Boolean).join(' · ') }),
  );

  const calcCard = h('section', { class: 'card card-pad' },
    h('h2', { class: 'card-title', text: 'Расчёт' }),
    rows.length
      ? h('table', { class: 'calc-table' }, h('tbody', {},
        ...rows.map((r) => h('tr', {}, h('td', { text: r.label }), h('td', { text: r.value })))))
      : h('div', { class: 'hint', text: 'Строк расчёта нет' }),
    h('div', { class: 'total' },
      h('span', { class: 'total-label', text: 'Итого' }),
      h('span', { class: 'total-value', text: fmtRub(d.total_rub) })),
  );

  const editCard = h('section', { class: 'card card-pad' },
    h('h2', { class: 'card-title', text: 'Правка' }),
    h('label', { class: 'field' },
      h('span', { class: 'field-label', text: 'Контрагент' }),
      h('input', {
        class: 'input', type: 'text', value: val('counterparty', data.counterparty),
        'data-focus-key': 'h-counterparty',
        oninput: (e) => setEdit('counterparty', e.target.value),
      })),
    h('div', { class: 'field' },
      h('span', { class: 'field-label', text: 'Выкуп' }),
      h('div', { class: 'row' },
        h('button', {
          class: 'choice', type: 'button', 'aria-pressed': bb.mode === 'pct' ? 'true' : 'false',
          style: { flex: '0 0 56px' }, text: '%',
          onclick: () => { st.edits.buyback = { mode: 'pct', value: bb.value }; render(); },
        }),
        h('button', {
          class: 'choice', type: 'button', 'aria-pressed': bb.mode === 'fixed' ? 'true' : 'false',
          style: { flex: '0 0 56px' }, text: '€',
          onclick: () => { st.edits.buyback = { mode: 'fixed', value: bb.value }; render(); },
        }),
        h('input', {
          class: 'input num grow', type: 'text', inputmode: 'decimal', value: String(bb.value || ''),
          'data-focus-key': 'h-buyback',
          oninput: (e) => { st.edits.buyback = { mode: bb.mode, value: toNum(e.target.value) }; },
        }),
      )),
    ...dirKeys.map((k) => h('label', { class: 'field' },
      h('span', { class: 'field-label', text: FIELD_LABELS[k] }),
      h('input', {
        class: 'input num', type: 'text', inputmode: 'decimal',
        value: String(val(k, data[k] ?? '')),
        'data-focus-key': `h-${k}`,
        oninput: (e) => setEdit(k, e.target.value),
      }))),
    st.detailError ? h('div', { class: 'mb8' }, errBox(st.detailError)) : null,
    h('button', {
      class: 'btn btn-wide', type: 'button',
      text: st.saving ? 'Сохраняем…' : 'Сохранить',
      disabled: st.saving,
      onclick: save,
    }),
  );

  return frag(back, header, calcCard, editCard);
}

async function save() {
  const keys = Object.keys(st.edits);
  if (!keys.length) { toast('Нечего сохранять — ничего не менялось'); return; }
  st.saving = true; st.detailError = '';
  render();
  try {
    let last = null;
    for (const key of keys) {
      const raw = st.edits[key];
      const value = (key === 'counterparty' || key === 'buyback') ? raw : toNum(raw);
      last = await api.historyUpdate(st.openId, key, value);
    }
    st.edits = {};
    if (last && (last.rows || last.total_rub !== undefined)) {
      st.detail = { ...st.detail, ...last };
    } else {
      await loadDetail();
    }
    toast('Сохранено', 'ok');
    haptic.ok();
  } catch (e) {
    st.detailError = e.message;
    toast(e.message, 'error');
    haptic.err();
  } finally {
    st.saving = false;
    render();
  }
}

async function sendKp() {
  if (st.sending) return;
  st.sending = true;
  render();
  try {
    await api.historyKp(st.openId);
    toast('КП отправлено в чат', 'ok');
    haptic.ok();
  } catch (e) {
    toast(e.message, 'error');
    haptic.err();
  } finally {
    st.sending = false;
    render();
  }
}

function syncMainButton() {
  if (!st.openId || !st.detail) { mainButton.hide(); return; }
  mainButton.set({
    text: 'Отправить КП заново',
    onClick: sendKp,
    enabled: !st.sending && !st.saving,
    loading: st.sending,
  });
}
