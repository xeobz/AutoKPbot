// Тонкая обёртка над Telegram.WebApp: главная кнопка, вибро, тема, закрытие.
// Вне Telegram всё деградирует до своей кнопки внизу экрана и пустых заглушек.

const wa = (() => {
  try { return (window.Telegram && window.Telegram.WebApp) || null; } catch { return null; }
})();

/** Реально ли мы внутри Telegram (скрипт грузится и в браузере, но platform там 'unknown'). */
export const isTelegram = !!(wa && ((wa.platform && wa.platform !== 'unknown') || wa.initData));

export function initTg() {
  if (!wa) return;
  try {
    wa.ready();
    wa.expand();
    applyScheme();
    if (typeof wa.onEvent === 'function') wa.onEvent('themeChanged', applyScheme);
  } catch { /* в браузере может не быть части API */ }
}

function applyScheme() {
  try {
    if (wa && wa.colorScheme) document.documentElement.setAttribute('data-tg-scheme', wa.colorScheme);
  } catch { /* игнорируем */ }
}

export function closeApp() {
  try { if (wa && typeof wa.close === 'function') wa.close(); } catch { /* игнорируем */ }
}

/* ---------- Вибро ---------- */
const hf = () => (wa && wa.HapticFeedback) || null;
export const haptic = {
  select() { try { hf() && hf().selectionChanged(); } catch { /* нет поддержки */ } },
  ok() { try { hf() && hf().notificationOccurred('success'); } catch { /* нет поддержки */ } },
  err() { try { hf() && hf().notificationOccurred('error'); } catch { /* нет поддержки */ } },
};

/* ---------- Главная кнопка ---------- */
const useNative = !!(isTelegram && wa && wa.MainButton);
let tgHandler = null;   // текущий обработчик нативной кнопки
let ownHandler = null;  // текущий обработчик своей кнопки

const ownWrap = () => document.getElementById('mainbtn-wrap');
const ownBtn = () => document.getElementById('mainbtn');

export const mainButton = {
  /** set({text, onClick, enabled, loading}) — одинаково работает и в TG, и в браузере. */
  set({ text, onClick, enabled = true, loading = false }) {
    if (useNative) {
      const mb = wa.MainButton;
      try {
        mb.setText(text || '');
        if (tgHandler) { mb.offClick(tgHandler); tgHandler = null; }
        if (onClick) { tgHandler = () => onClick(); mb.onClick(tgHandler); }
        if (loading) mb.showProgress(false); else mb.hideProgress();
        if (enabled && !loading) mb.enable(); else mb.disable();
        mb.show();
      } catch { /* игнорируем */ }
      return;
    }
    const wrap = ownWrap();
    const btn = ownBtn();
    if (!wrap || !btn) return;
    btn.textContent = loading ? 'Подождите…' : (text || '');
    btn.disabled = !enabled || loading;
    if (ownHandler) btn.removeEventListener('click', ownHandler);
    ownHandler = onClick ? () => onClick() : null;
    if (ownHandler) btn.addEventListener('click', ownHandler);
    wrap.hidden = false;
    document.body.classList.add('has-mainbtn');
  },

  hide() {
    if (useNative) {
      try {
        if (tgHandler) { wa.MainButton.offClick(tgHandler); tgHandler = null; }
        wa.MainButton.hideProgress();
        wa.MainButton.hide();
      } catch { /* игнорируем */ }
      return;
    }
    const wrap = ownWrap();
    const btn = ownBtn();
    if (btn && ownHandler) { btn.removeEventListener('click', ownHandler); ownHandler = null; }
    if (wrap) wrap.hidden = true;
    document.body.classList.remove('has-mainbtn');
  },
};
