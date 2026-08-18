// Точка входа: инициализация Telegram, профиль, нижние вкладки, монтирование экранов.
import { api } from './api.js?v=2';
import { initTg, mainButton, haptic } from './tg.js?v=2';
import { h, stateLoading, stateError } from './ui.js?v=2';
import { renderCalc } from './calc.js?v=2';
import { renderHistory } from './history.js?v=2';
import { renderSettings } from './settings.js?v=2';
import { renderDraft } from './draft.js?v=2';

const appEl = document.getElementById('app');
const tabbarEl = document.getElementById('tabbar');
const topbarEl = document.getElementById('topbar');
const topbarInfoEl = document.getElementById('topbar-info');

let me = null;

/** Курс дня и профиль нужны экрану расчёта — отдаём наружу. */
export const getMe = () => me;
let currentTab = 'calc';

boot();

async function boot() {
  initTg();

  const draftId = new URLSearchParams(location.search).get('draft');
  appEl.replaceChildren(stateLoading('Загружаем…'));

  try {
    me = await api.me();
  } catch (e) {
    // На экране выбора фото профиль не критичен — идём дальше
    if (!draftId) {
      appEl.replaceChildren(stateError(e.message, boot));
      return;
    }
  }

  renderTopbar();

  // Открытие из бота на шаге выбора фото — сразу отдельный экран, без вкладок
  if (draftId) {
    tabbarEl.hidden = true;
    document.body.classList.remove('has-tabbar');
    mount((el) => renderDraft(el, draftId));
    return;
  }

  setupTabs();
  openTab('calc');
}

/* ---------- Шапка ---------- */

function renderTopbar() {
  if (!me) { topbarEl.hidden = true; return; }
  topbarEl.hidden = false;
  const r = me.rates || {};
  topbarInfoEl.replaceChildren(
    h('span', { text: me.name || '' }),
    r.rate_eur_usdt ? h('span', { text: ' · ' }) : null,
    r.rate_eur_usdt ? h('span', {
      class: r.is_today ? '' : 'warn-dot',
      text: `${r.rate_eur_usdt} / ${r.rate_usdt_rub}${r.is_today ? '' : ' ⚠️'}`,
    }) : null,
  );
}

/* ---------- Вкладки ---------- */

function setupTabs() {
  tabbarEl.hidden = false;
  document.body.classList.add('has-tabbar');
  for (const btn of tabbarEl.querySelectorAll('.tab')) {
    if (btn.dataset.tab === 'settings') btn.hidden = !(me && me.is_admin);
    btn.addEventListener('click', () => {
      if (currentTab === btn.dataset.tab) return;
      haptic.select();
      openTab(btn.dataset.tab);
    });
  }
}

function openTab(name) {
  currentTab = name;
  for (const btn of tabbarEl.querySelectorAll('.tab')) {
    btn.classList.toggle('active', btn.dataset.tab === name);
  }
  mainButton.hide(); // экран выставит свою кнопку сам
  window.scrollTo({ top: 0 });

  if (name === 'history') mount((el) => renderHistory(el));
  else if (name === 'settings') mount((el) => renderSettings(el, me, renderTopbar));
  else mount((el) => renderCalc(el));
}

/** Каждый экран живёт в своём контейнере: старый отцепляется, его таймеры затихают. */
function mount(fn) {
  const box = h('div');
  appEl.replaceChildren(box);
  fn(box);
}
