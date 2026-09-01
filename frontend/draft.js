// Экран выбора фото по ссылке из бота (?draft=<id>) — без вкладок, одна задача.
import { api } from './api.js?v=3';
import {
  h, frag, fmtEur, toast, stateLoading, stateError,
} from './ui.js?v=3';
import { mainButton, haptic, closeApp } from './tg.js?v=3';
import { photoGrid, galleryBar } from './gallery.js?v=3';

let st = {
  id: null, data: null, loading: false, error: '',
  selected: [], sending: false, done: false,
};
let mountRoot = null;

export function renderDraft(root, draftId) {
  mountRoot = root;
  st.id = draftId;
  render();
  if (!st.data && !st.loading) load();
}

function render() {
  if (!mountRoot || !mountRoot.isConnected) return;
  mountRoot.replaceChildren(build());
  syncMainButton();
}

async function load() {
  st.loading = true; st.error = '';
  render();
  try {
    const d = await api.draft(st.id);
    st.data = d || {};
    const all = st.data.photos || [];
    // отмечаем уже выбранные, порядок берём из preselected
    st.selected = (st.data.preselected || []).filter((u) => all.includes(u));
  } catch (e) {
    st.error = e.message;
  } finally {
    st.loading = false;
    render();
  }
}

function build() {
  if (st.done) {
    return h('section', { class: 'card card-pad center' },
      h('div', { style: { fontSize: '40px' }, text: '✅' }),
      h('h2', { class: 'car-title mt8', text: 'Готово' }),
      h('div', { class: 'hint', text: 'КП отправлено в чат' }),
    );
  }
  if (st.loading && !st.data) return stateLoading('Открываем черновик…');
  if (st.error && !st.data) return stateError(st.error, load);
  if (!st.data) return stateError('Черновик не найден', load);

  const car = st.data.car || {};
  const all = st.data.photos || [];

  return frag(
    h('section', { class: 'card card-pad' },
      h('h2', { class: 'car-title', text: car.title || 'Выбор фото' }),
      h('div', { class: 'car-meta', text: [st.data.car_num ? `Запись #${st.data.car_num}` : null, car.price_eur ? fmtEur(car.price_eur) : null].filter(Boolean).join(' · ') }),
    ),
    h('section', { class: 'card card-pad' },
      h('h2', { class: 'card-title', text: 'Фото для КП' }),
      all.length
        ? frag(
          galleryBar({
            selectedCount: st.selected.length,
            total: all.length,
            onClear: () => { st.selected = []; render(); },
          }),
          photoGrid({
            photos: all,
            selected: st.selected,
            onChange: (next) => { st.selected = next; render(); },
          }),
          h('div', { class: 'hint small mt8', text: 'Порядок важен: первое фото несёт текст КП.' }),
        )
        : h('div', { class: 'hint', text: 'Фотографий нет' }),
    ),
  );
}

function syncMainButton() {
  if (st.done || !st.data || !st.selected.length) { mainButton.hide(); return; }
  mainButton.set({
    text: 'Отправить КП',
    onClick: send,
    enabled: !st.sending,
    loading: st.sending,
  });
}

async function send() {
  if (st.sending) return;
  st.sending = true;
  render();
  try {
    await api.draftPhotos(st.id, st.selected);
    st.done = true;
    haptic.ok();
    render();
    setTimeout(closeApp, 1000); // в браузере просто ничего не произойдёт
  } catch (e) {
    toast(e.message, 'error');
    haptic.err();
  } finally {
    st.sending = false;
    render();
  }
}
