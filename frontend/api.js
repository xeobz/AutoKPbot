// Обёртка над fetch: относительные пути, заголовок X-Init-Data, человеческие ошибки.

export class ApiError extends Error {
  constructor(message, status = 0, data = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

function initData() {
  try {
    return (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) || '';
  } catch {
    return '';
  }
}

function humanError(status, data) {
  const d = data && data.detail;
  if (typeof d === 'string' && d.trim()) return d.trim();
  if (Array.isArray(d)) {
    const msgs = d.map((x) => (x && (x.msg || x.detail)) || '').filter(Boolean);
    if (msgs.length) return msgs.join('; ');
  }
  if (d && typeof d === 'object' && typeof d.message === 'string') return d.message;
  if (typeof (data && data.message) === 'string') return data.message;

  if (status === 401) return 'Не удалось подтвердить вход. Откройте мини-апп заново из чата с ботом.';
  if (status === 403) return 'Недостаточно прав: раздел доступен только администратору.';
  if (status === 404) return 'Данные не найдены — возможно, устарела ссылка.';
  if (status === 409) return 'Данные уже изменены кем-то ещё.';
  if (status === 422) return 'Сервер не принял данные формы.';
  if (status >= 500) return `Сервер ответил ошибкой (${status}). Попробуйте ещё раз через минуту.`;
  return `Запрос не прошёл (код ${status}).`;
}

// Сколько ждём ответа. Разбор объявления и сборка КП идут десятки секунд —
// им нужен запас, остальному хватает половины минуты. Без предела запрос
// в мобильной сети может висеть бесконечно, и приложение выглядит зависшим.
const TIMEOUTS = [
  [/\/api\/parse/, 180000],
  [/\/api\/(preview|submit)/, 150000],
];
const DEFAULT_TIMEOUT = 30000;

function timeoutFor(path) {
  const hit = TIMEOUTS.find(([re]) => re.test(path));
  return hit ? hit[1] : DEFAULT_TIMEOUT;
}

export async function request(path, { method = 'GET', body } = {}) {
  const headers = {};
  const id = initData();
  if (id) headers['X-Init-Data'] = id; // в браузере заголовка нет — сервер пустит в dev-режиме
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const ctrl = AbortController ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutFor(path)) : null;

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl ? ctrl.signal : undefined,
    });
  } catch (e) {
    if (e && e.name === 'AbortError') {
      // Отправку могло и выполнить: сервер пишет строку и шлёт КП дольше,
      // чем мы готовы ждать. Поэтому не «ошибка», а «проверьте чат».
      throw new ApiError(
        /submit/.test(path)
          ? 'Ответ не пришёл вовремя. Проверьте чат с ботом и историю — запись могла пройти.'
          : 'Сервер долго не отвечает. Попробуйте ещё раз.',
        0,
      );
    }
    throw new ApiError('Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.', 0);
  } finally {
    if (timer) clearTimeout(timer);
  }

  const text = await res.text().catch(() => '');
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch { data = null; } }

  if (!res.ok) throw new ApiError(humanError(res.status, data), res.status, data);
  return data;
}

const get = (p) => request(p);
const post = (p, body) => request(p, { method: 'POST', body: body || {} });

export const api = {
  me: () => get('/api/me'),
  parse: (url) => post('/api/parse', { url }),
  calc: (payload) => post('/api/calc', payload),
  preview: (payload) => post('/api/preview', payload),
  submit: (payload) => post('/api/submit', payload),

  history: (limit = 20) => get(`/api/history?limit=${encodeURIComponent(limit)}`),
  historyItem: (id) => get(`/api/history/${encodeURIComponent(id)}`),
  historyUpdate: (id, field, value) => post(`/api/history/${encodeURIComponent(id)}`, { field, value }),
  historyKp: (id) => post(`/api/history/${encodeURIComponent(id)}/kp`),

  settings: () => get('/api/settings'),
  settingsSave: (key, value) => post('/api/settings', { key, value }),
  ratesSave: (rate_eur_usdt, rate_usdt_rub) => post('/api/rates', { rate_eur_usdt, rate_usdt_rub }),

  draft: (id) => get(`/api/draft/${encodeURIComponent(id)}`),
  draftPhotos: (id, photos) => post(`/api/draft/${encodeURIComponent(id)}/photos`, { photos }),
};
