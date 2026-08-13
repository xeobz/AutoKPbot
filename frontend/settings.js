// Экран «Настройки» (только админ): разделы с полями, отдельный блок «Курс дня».
import { api } from './api.js';
import {
  h, frag, icon, toNum, toast,
  stateLoading, stateEmpty, stateError, errBox, keepFocus,
} from './ui.js';
import { haptic, mainButton } from './tg.js';

let st = {
  data: null, loading: false, error: '',
  values: {},      // key → введённое значение
  savingKey: null,
  rates: null,     // {eur, usdt} — заполняем при первой отрисовке
  ratesSaving: false, ratesError: '',
};
let mountRoot = null;
let meRef = null;
let onRatesSaved = null;

export function renderSettings(root, me, onRates) {
  mountRoot = root;
  meRef = me;
  onRatesSaved = onRates;
  mainButton.hide(); // на этом экране главной кнопки нет
  render();
  if (!st.data && !st.loading) load();
}

function render() {
  if (!mountRoot || !mountRoot.isConnected) return;
  keepFocus(() => mountRoot.replaceChildren(build()));
}

async function load() {
  st.loading = true; st.error = '';
  render();
  try {
    st.data = await api.settings();
    st.values = {};
    for (const s of (st.data.sections || [])) {
      for (const it of (s.items || [])) st.values[it.key] = it.value ?? '';
    }
    st.rates = null; // перечитаем из свежих данных
  } catch (e) {
    st.error = e.message;
  } finally {
    st.loading = false;
    render();
  }
}

function build() {
  if (!meRef || !meRef.is_admin) {
    return stateEmpty('Раздел только для админов', 'Попросите доступ у владельца бота.');
  }
  if (st.loading && !st.data) return stateLoading('Загружаем настройки…');
  if (st.error && !st.data) return stateError(st.error, load);
  const sections = (st.data && st.data.sections) || [];
  if (!sections.length) return stateEmpty('Настроек нет');

  // «Курс дня» всегда первым — он самый важный
  const rates = sections.filter((s) => s.key === 'rates');
  const rest = sections.filter((s) => s.key !== 'rates');

  return frag(
    ...rates.map(ratesCard),
    ...rest.map(sectionCard),
    emojiStub(),
  );
}

/* ---------- Курс дня ---------- */

function ratesCard(section) {
  if (!st.rates) {
    const items = section.items || [];
    const pick = (k) => {
      const it = items.find((x) => x.key === k);
      if (it && it.value !== undefined && it.value !== null && it.value !== '') return String(it.value);
      const fromMe = meRef && meRef.rates ? meRef.rates[k] : null;
      return fromMe !== null && fromMe !== undefined ? String(fromMe) : '';
    };
    st.rates = { rate_eur_usdt: pick('rate_eur_usdt'), rate_usdt_rub: pick('rate_usdt_rub') };
  }

  const r = (meRef && meRef.rates) || {};
  const isToday = !!r.is_today;

  return h('section', { class: 'card card-pad' },
    h('h2', { class: 'card-title', text: section.title || 'Курс дня' }),
    isToday
      ? h('div', { class: 'ok-box mb8', text: `Курс на ${r.rates_date || 'сегодня'} задан: ${r.rate_eur_usdt} EUR→USDT · ${r.rate_usdt_rub} USDT→₽` })
      : h('div', { class: 'warn-box mb8', text: 'Курс на сегодня не задан. Расчёты пойдут по старому курсу — задайте актуальный.' }),
    h('label', { class: 'field' },
      h('span', { class: 'field-label', text: 'Курс EUR→USDT' }),
      h('input', {
        class: 'input num', type: 'text', inputmode: 'decimal', placeholder: '1.16',
        value: st.rates.rate_eur_usdt, 'data-focus-key': 'rate_eur_usdt',
        oninput: (e) => { st.rates.rate_eur_usdt = e.target.value; },
      })),
    h('label', { class: 'field' },
      h('span', { class: 'field-label', text: 'Курс USDT→₽' }),
      h('input', {
        class: 'input num', type: 'text', inputmode: 'decimal', placeholder: '79.7',
        value: st.rates.rate_usdt_rub, 'data-focus-key': 'rate_usdt_rub',
        oninput: (e) => { st.rates.rate_usdt_rub = e.target.value; },
      })),
    st.ratesError ? h('div', { class: 'mb8' }, errBox(st.ratesError)) : null,
    h('button', {
      class: 'btn btn-wide', type: 'button',
      text: st.ratesSaving ? 'Сохраняем…' : 'Сохранить курс',
      disabled: st.ratesSaving,
      onclick: saveRates,
    }),
  );
}

async function saveRates() {
  const eur = toNum(st.rates.rate_eur_usdt);
  const usdt = toNum(st.rates.rate_usdt_rub);
  if (!eur || !usdt) { toast('Заполните оба курса', 'error'); return; }
  st.ratesSaving = true; st.ratesError = '';
  render();
  try {
    const r = await api.ratesSave(eur, usdt);
    if (meRef) {
      meRef.rates = {
        ...(meRef.rates || {}),
        rate_eur_usdt: eur,
        rate_usdt_rub: usdt,
        is_today: true,
        ...(r && typeof r === 'object' ? r : {}),
      };
    }
    toast('Курс сохранён', 'ok');
    haptic.ok();
    if (onRatesSaved) onRatesSaved();
  } catch (e) {
    // 409 — курс на сегодня уже выставил кто-то другой, показываем текст сервера
    st.ratesError = e.message;
    haptic.err();
  } finally {
    st.ratesSaving = false;
    render();
  }
}

/* ---------- Остальные разделы ---------- */

function sectionCard(section) {
  const items = section.items || [];
  return h('section', { class: 'card card-pad' },
    h('h2', { class: 'card-title', text: section.title || section.key }),
    items.length
      ? frag(...items.map(settingItem))
      : h('div', { class: 'hint', text: 'Пусто' }),
  );
}

function settingItem(item) {
  const saving = st.savingKey === item.key;
  // Кнопка оживает только у изменённого значения — иначе это ряд одинаковых «ОК»
  const changed = String(st.values[item.key] ?? '').trim() !== String(item.value ?? '').trim();
  return h('div', { class: 'field' },
    h('span', { class: 'field-label', text: item.unit ? `${item.label}, ${item.unit}` : item.label }),
    h('div', { class: 'row' },
      h('input', {
        class: 'input num grow', type: 'text', inputmode: 'decimal',
        value: st.values[item.key] ?? '',
        'data-focus-key': `set-${item.key}`,
        oninput: (e) => { st.values[item.key] = e.target.value; render(); },
      }),
      h('button', {
        class: 'btn btn-sm', type: 'button', style: { minHeight: '44px' },
        text: saving ? '…' : 'Сохранить',
        disabled: saving || !changed,
        onclick: () => saveItem(item),
      }),
    ),
  );
}

async function saveItem(item) {
  const raw = st.values[item.key];
  const value = /^-?[\d\s.,]+$/.test(String(raw).trim()) ? toNum(raw) : raw;
  st.savingKey = item.key;
  render();
  try {
    const r = await api.settingsSave(item.key, value);
    // Обновляем исходное значение, иначе кнопка останется активной
    item.value = (r && r.value !== undefined) ? String(r.value) : String(value);
    st.values[item.key] = item.value;
    toast(`${item.label}: сохранено`, 'ok');
    haptic.ok();
  } catch (e) {
    toast(e.message, 'error');
    haptic.err();
  } finally {
    st.savingKey = null;
    render();
  }
}

/* ---------- Заглушка про эмодзи ---------- */

function emojiStub() {
  return h('section', { class: 'card card-pad' },
    h('div', { class: 'row' },
      icon('sticker', 20),
      h('span', { class: 'hint', text: 'Эмодзи марок настраиваются в чате с ботом' }),
    ),
  );
}
