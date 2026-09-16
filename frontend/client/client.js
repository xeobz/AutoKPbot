// Конструктор презентации: в макете выбирается рамка, снизу — снимок для неё,
// пальцем двигается кадр, ползунком — масштаб. Превью — та же разметка,
// что уйдёт в Chrome на печать, поэтому что видно здесь, то и будет в файле.
(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const jobId = new URLSearchParams(location.search).get('job') || '';
  const $ = (id) => document.getElementById(id);
  const SIZE = { pdf: [1600, 900], story: [1080, 1920] };

  const st = {
    data: null, fmt: null, layout: {}, selected: null,
    doc: null, scale: 1, saving: false,
  };

  // ── API ─────────────────────────────────────────────────────────────────
  async function api(path, opts) {
    const res = await fetch('/api/client' + path, Object.assign({
      headers: { 'Content-Type': 'application/json', 'X-Init-Data': (tg && tg.initData) || '' },
    }, opts || {}));
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) throw new Error((body && body.detail) || 'Сервер не ответил, попробуйте ещё раз');
    return body;
  }

  function showError(text) {
    $('error').textContent = text;
    $('error').hidden = !text;
  }

  // ── Загрузка задания ────────────────────────────────────────────────────
  async function boot() {
    if (!jobId) { showError('Откройте конструктор кнопкой в боте'); return; }
    try {
      st.data = await api('/jobs/' + encodeURIComponent(jobId));
    } catch (e) {
      $('title').textContent = 'Не удалось открыть';
      $('loading').textContent = e.message;
      return;
    }
    const d = st.data;
    $('title').textContent = d.title;
    $('price').textContent = d.price;
    st.layout = JSON.parse(JSON.stringify(d.layout || {}));

    if (d.formats.length > 1) {
      const tabs = $('tabs');
      tabs.hidden = false;
      d.formats.forEach((f) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = f === 'pdf' ? '📄 PDF' : '📱 Сторис';
        b.onclick = () => switchFormat(f);
        b.dataset.fmt = f;
        tabs.appendChild(b);
      });
    }
    buildStrip();
    $('saveBtn').disabled = false;
    switchFormat(d.formats[0]);
    window.addEventListener('resize', fitFrame);
  }

  function switchFormat(fmt) {
    st.fmt = fmt;
    select(null);
    document.querySelectorAll('#tabs button').forEach((b) =>
      b.setAttribute('aria-pressed', b.dataset.fmt === fmt ? 'true' : 'false'));
    loadPreview();
  }

  // ── Превью ──────────────────────────────────────────────────────────────
  function loadPreview() {
    const frame = $('frame');
    const [w] = SIZE[st.fmt];
    $('loading').hidden = false;
    $('loading').textContent = 'Собираем превью…';
    frame.style.width = w + 'px';
    frame.onload = () => waitReady(frame, 0);
    frame.src = '/api/client/jobs/' + encodeURIComponent(jobId) + '/preview/' + st.fmt + '?t=' + Date.now();
  }

  function waitReady(frame, tries) {
    const doc = frame.contentDocument;
    // комплектация раскладывается скриптом после загрузки шрифтов — ждём её
    if (!doc || !doc.body || (doc.body.dataset.ready !== '1' && tries < 60)) {
      setTimeout(() => waitReady(frame, tries + 1), 100);
      return;
    }
    st.doc = doc;
    applyLayout();
    bindSlots();
    fitFrame();
    $('loading').hidden = true;
  }

  function fitFrame() {
    if (!st.doc) return;
    const [w, h0] = SIZE[st.fmt];
    const h = st.fmt === 'pdf' ? Math.max(h0, st.doc.body.scrollHeight) : h0;
    const frame = $('frame');
    st.scale = $('frameWrap').clientWidth / w;
    frame.style.height = h + 'px';
    frame.style.transform = 'scale(' + st.scale + ')';
    $('frameWrap').style.height = Math.ceil(h * st.scale) + 'px';
  }

  function photoUrl(idx) {
    return '/api/client/jobs/' + encodeURIComponent(jobId) + '/photo/' + idx;
  }

  // Раскладка из памяти переносится в превью: вкладку перезагрузили,
  // а правки контрагента терять нельзя
  function applyLayout() {
    st.doc.querySelectorAll('[data-slot]').forEach((el) => {
      const s = st.layout[el.dataset.slot];
      if (s) paint(el, s);
    });
  }

  function paint(el, s) {
    const img = el.querySelector('img');
    const src = photoUrl(s.photo);
    if (!img.src.endsWith(src)) img.src = src;
    img.style.objectPosition = s.x.toFixed(1) + '% ' + s.y.toFixed(1) + '%';
    img.style.transform = 'scale(' + s.zoom.toFixed(3) + ')';
    img.style.transformOrigin = s.x.toFixed(1) + '% ' + s.y.toFixed(1) + '%';
  }

  // ── Выбор рамки и перетаскивание кадра ──────────────────────────────────
  function bindSlots() {
    st.doc.querySelectorAll('[data-slot]').forEach((el) => {
      let drag = null;
      el.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        select(el.dataset.slot);
        const img = el.querySelector('img');
        const s = st.layout[el.dataset.slot];
        drag = { x: e.clientX, y: e.clientY, sx: s.x, sy: s.y, img: img, el: el };
        el.setPointerCapture(e.pointerId);
        el.style.cursor = 'grabbing';
      });
      el.addEventListener('pointermove', (e) => {
        if (!drag) return;
        const s = st.layout[el.dataset.slot];
        const box = el.getBoundingClientRect();
        const img = drag.img;
        const nw = img.naturalWidth || box.width, nh = img.naturalHeight || box.height;
        // насколько кадр больше рамки — столько и можно сдвинуть
        const fit = Math.max(box.width / nw, box.height / nh) * s.zoom;
        const spareX = nw * fit - box.width, spareY = nh * fit - box.height;
        if (spareX > 1) s.x = clamp(drag.sx - (e.clientX - drag.x) / spareX * 100, 0, 100);
        if (spareY > 1) s.y = clamp(drag.sy - (e.clientY - drag.y) / spareY * 100, 0, 100);
        paint(el, s);
      });
      const end = () => { drag = null; el.style.cursor = ''; };
      el.addEventListener('pointerup', end);
      el.addEventListener('pointercancel', end);
    });
  }

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  function select(name) {
    st.selected = name;
    if (st.doc) {
      st.doc.querySelectorAll('[data-slot]').forEach((el) =>
        el.classList.toggle('sel', el.dataset.slot === name));
    }
    const slots = (st.data && st.data.slots[st.fmt]) || [];
    const meta = slots.find((x) => x.name === name);
    $('slotName').textContent = meta ? meta.title : 'Выберите фото в макете';
    const s = name && st.layout[name];
    $('zoom').disabled = !s;
    $('resetBtn').disabled = !s;
    $('zoom').value = s ? s.zoom : 1;
    $('strip').classList.toggle('active', !!s);
    markStrip();
  }

  // ── Лента снимков ───────────────────────────────────────────────────────
  function buildStrip() {
    const strip = $('strip');
    st.data.photos.forEach((p) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.idx = p.idx;
      b.innerHTML = '<img loading="lazy" alt="">';
      b.querySelector('img').src = p.thumb;
      b.onclick = () => choosePhoto(p.idx);
      strip.appendChild(b);
    });
  }

  function markStrip() {
    const current = st.selected && st.layout[st.selected];
    document.querySelectorAll('#strip button').forEach((b) => {
      const idx = +b.dataset.idx;
      b.setAttribute('aria-pressed', current && current.photo === idx ? 'true' : 'false');
    });
  }

  function choosePhoto(idx) {
    if (!st.selected || !st.layout[st.selected]) {
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('warning');
      $('hint').textContent = 'Сначала нажмите на фото в макете — потом выберите снимок для него.';
      return;
    }
    // новый снимок — сдвиг и масштаб от старого ему не подходят
    Object.assign(st.layout[st.selected], { photo: idx, zoom: 1, x: 50, y: 50 });
    const el = st.doc.querySelector('[data-slot="' + st.selected + '"]');
    if (el) paint(el, st.layout[st.selected]);
    $('zoom').value = 1;
    markStrip();
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  }

  $('zoom').addEventListener('input', (e) => {
    const s = st.selected && st.layout[st.selected];
    if (!s) return;
    s.zoom = +e.target.value;
    const el = st.doc.querySelector('[data-slot="' + st.selected + '"]');
    if (el) paint(el, s);
  });

  $('resetBtn').addEventListener('click', () => {
    const s = st.selected && st.layout[st.selected];
    if (!s) return;
    Object.assign(s, { zoom: 1, x: 50, y: 50 });
    const el = st.doc.querySelector('[data-slot="' + st.selected + '"]');
    if (el) paint(el, s);
    $('zoom').value = 1;
  });

  // ── Сохранение ──────────────────────────────────────────────────────────
  $('saveBtn').addEventListener('click', async () => {
    if (st.saving) return;
    st.saving = true;
    showError('');
    const btn = $('saveBtn');
    btn.disabled = true;
    btn.textContent = 'Отправляем…';
    try {
      await api('/jobs/' + encodeURIComponent(jobId) + '/submit', {
        method: 'POST', body: JSON.stringify({ layout: st.layout }),
      });
      btn.textContent = '✅ Готово — файлы придут в чат';
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
      setTimeout(() => { if (tg) tg.close(); }, 1400);
    } catch (e) {
      showError(e.message);
      btn.disabled = false;
      btn.textContent = 'Сохранить и получить файлы';
      st.saving = false;
    }
  });

  boot();
})();
