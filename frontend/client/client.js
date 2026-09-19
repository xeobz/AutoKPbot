// Конструктор презентации. Сверху — карусель дизайнов с живыми обложками,
// ниже — макет: нажатие на фото открывает панель со снимками. Превью — та же
// разметка, что уходит в Chrome на печать: что видно здесь, то и будет в файле.
(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const jobId = new URLSearchParams(location.search).get('job') || '';
  const $ = (id) => document.getElementById(id);
  const STORY = [1080, 1920];

  const st = { data: null, fmt: null, design: 'classic', layout: {}, selected: null, doc: null, scale: 1, saving: false };

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

  function haptic(kind) {
    if (!tg || !tg.HapticFeedback) return;
    if (kind === 'select') tg.HapticFeedback.selectionChanged();
    else tg.HapticFeedback.notificationOccurred(kind);
  }

  function designMeta(id) {
    return (st.data.designs || []).find((x) => x.id === id) || { id: 'classic', title: 'Оригинал', pdf_size: [1600, 900] };
  }

  // Размер страницы зависит от дизайна: «Оригинал» 16:9, референсы 4:3
  function pageSize(fmt, design) {
    return fmt === 'story' ? STORY : designMeta(design || st.design).pdf_size;
  }

  function previewUrl(fmt, design, cover) {
    return '/api/client/jobs/' + encodeURIComponent(jobId) + '/preview/' + fmt +
      '?design=' + encodeURIComponent(design) + (cover ? '&cover=1' : '') + '&t=' + Date.now();
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
    st.design = d.design || 'classic';
    st.layout._design = { id: st.design };

    if (d.formats.length > 1) {
      $('tabs').hidden = false;
      d.formats.forEach((f) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.dataset.fmt = f;
        b.textContent = f === 'pdf' ? 'PDF' : 'Сторис';
        b.onclick = () => switchFormat(f);
        $('tabs').appendChild(b);
      });
    }
    buildStrip();
    $('saveBtn').disabled = false;
    switchFormat(d.formats[0]);
    window.addEventListener('resize', () => { fitFrame(); fitThumbs(); });
  }

  function switchFormat(fmt) {
    st.fmt = fmt;
    closeSheet();
    document.querySelectorAll('#tabs button').forEach((b) =>
      b.setAttribute('aria-pressed', b.dataset.fmt === fmt ? 'true' : 'false'));
    buildDesigns();
    loadPreview();
  }

  // ── Карусель дизайнов ───────────────────────────────────────────────────
  function buildDesigns() {
    const designs = st.data.designs || [];
    $('designs').hidden = designs.length < 2;
    const track = $('designTrack');
    track.innerHTML = '';
    designs.forEach((d) => {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'dcard' + (st.fmt === 'story' ? ' story' : '');
      card.dataset.id = d.id;
      card.setAttribute('aria-pressed', d.id === st.design ? 'true' : 'false');
      card.innerHTML = '<div class="thumb"><div class="ph">…</div><iframe tabindex="-1" scrolling="no"></iframe></div>' +
        '<div class="cap"><b></b><span></span></div>';
      card.querySelector('b').textContent = d.title;
      card.querySelector('.cap span').textContent = d.description || '';
      const frame = card.querySelector('iframe');
      frame.onload = () => { const ph = card.querySelector('.ph'); if (ph) ph.remove(); };
      frame.src = previewUrl(st.fmt, d.id, true);
      card.onclick = () => chooseDesign(d.id);
      track.appendChild(card);
    });
    requestAnimationFrame(() => {
      fitThumbs();
      const cur = track.querySelector('[aria-pressed="true"]');
      if (cur) cur.scrollIntoView({ inline: 'center', block: 'nearest' });
    });
    $('designName').textContent = designMeta(st.design).title;
  }

  function fitThumbs() {
    document.querySelectorAll('#designTrack .dcard').forEach((card) => {
      const [w, h] = pageSize(st.fmt, card.dataset.id);
      const thumb = card.querySelector('.thumb');
      const frame = card.querySelector('iframe');
      const scale = thumb.clientWidth / w;
      thumb.style.height = Math.round(h * scale) + 'px';
      frame.style.width = w + 'px';
      frame.style.height = h + 'px';
      frame.style.transform = 'scale(' + scale + ')';
    });
  }

  function chooseDesign(id) {
    if (id === st.design) return;
    st.design = id;
    st.layout._design = { id: id };
    document.querySelectorAll('#designTrack .dcard').forEach((c) =>
      c.setAttribute('aria-pressed', c.dataset.id === id ? 'true' : 'false'));
    $('designName').textContent = designMeta(id).title;
    haptic('select');
    closeSheet();
    loadPreview();
  }

  // ── Превью ──────────────────────────────────────────────────────────────
  function loadPreview() {
    const frame = $('frame');
    $('loading').hidden = false;
    $('loading').textContent = 'Собираем превью…';
    frame.style.width = pageSize(st.fmt)[0] + 'px';
    frame.onload = () => waitReady(frame, 0);
    frame.src = previewUrl(st.fmt, st.design, false);
  }

  function waitReady(frame, tries) {
    const doc = frame.contentDocument;
    // комплектация раскладывается скриптом после загрузки шрифта — ждём её
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
    const [w, h0] = pageSize(st.fmt);
    const h = st.fmt === 'pdf' ? Math.max(h0, st.doc.body.scrollHeight) : h0;
    st.scale = $('frameWrap').clientWidth / w;
    $('frame').style.height = h + 'px';
    $('frame').style.transform = 'scale(' + st.scale + ')';
    $('frameWrap').style.height = Math.ceil(h * st.scale) + 'px';
  }

  function photoUrl(idx) {
    return '/api/client/jobs/' + encodeURIComponent(jobId) + '/photo/' + idx;
  }

  // правки из памяти переносятся в заново загруженное превью (вкладка, дизайн)
  function applyLayout() {
    st.doc.querySelectorAll('[data-slot]').forEach((el) => {
      const s = st.layout[el.dataset.slot];
      if (s) paint(el, s);
    });
  }

  function paint(el, s) {
    const img = el.querySelector('img');
    const src = photoUrl(s.photo);
    if (!img.getAttribute('src').endsWith(src)) img.src = src;
    img.style.objectPosition = s.x.toFixed(1) + '% ' + s.y.toFixed(1) + '%';
    img.style.transform = 'scale(' + s.zoom.toFixed(3) + ')';
    img.style.transformOrigin = s.x.toFixed(1) + '% ' + s.y.toFixed(1) + '%';
  }

  function slotEl(name) {
    return st.doc && st.doc.querySelector('[data-slot="' + name + '"]');
  }

  // ── Нажатие и перетаскивание в макете ───────────────────────────────────
  function bindSlots() {
    st.doc.querySelectorAll('[data-slot]').forEach((el) => {
      let drag = null;
      el.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        const s = st.layout[el.dataset.slot];
        drag = { x: e.clientX, y: e.clientY, sx: s.x, sy: s.y, moved: false };
        el.setPointerCapture(e.pointerId);
      });
      el.addEventListener('pointermove', (e) => {
        if (!drag) return;
        // двигать кадр можно только в уже выбранной рамке: иначе при прокрутке
        // страницы пальцем фото случайно съезжали бы
        if (st.selected !== el.dataset.slot) return;
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) < 4) return;
        drag.moved = true;
        const s = st.layout[el.dataset.slot];
        const box = el.getBoundingClientRect();
        const img = el.querySelector('img');
        const nw = img.naturalWidth || box.width, nh = img.naturalHeight || box.height;
        // в дизайнах по референсам фото вписано в подложку целиком (contain):
        // двигать его есть смысл, только когда оно увеличено
        const contain = getComputedStyle(img).objectFit === 'contain';
        const base = contain ? Math.min(box.width / nw, box.height / nh) : Math.max(box.width / nw, box.height / nh);
        const fit = base * s.zoom;
        const spareX = nw * fit - box.width, spareY = nh * fit - box.height;
        if (spareX > 1) s.x = clamp(drag.sx - dx / spareX * 100, 0, 100);
        if (spareY > 1) s.y = clamp(drag.sy - dy / spareY * 100, 0, 100);
        paint(el, s);
      });
      const end = () => {
        if (drag && !drag.moved) openSheet(el.dataset.slot);
        drag = null;
      };
      el.addEventListener('pointerup', end);
      el.addEventListener('pointercancel', () => { drag = null; });
    });
  }

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  // ── Панель замены фото ──────────────────────────────────────────────────
  function openSheet(name) {
    st.selected = name;
    st.doc.querySelectorAll('[data-slot]').forEach((el) =>
      el.classList.toggle('sel', el.dataset.slot === name));
    const meta = (st.data.slots[st.fmt] || []).find((x) => x.name === name);
    $('slotName').textContent = meta ? meta.title : 'Фото';
    $('zoom').value = st.layout[name].zoom;
    paintZoom();
    markStrip(true);
    setTimeout(updateArrows, 300);
    $('sheet').classList.add('open');
    $('sheet').setAttribute('aria-hidden', 'false');
    document.body.classList.add('sheet-open');
    $('hint').textContent = 'Выберите снимок внизу. Двигайте фото пальцем, чтобы выровнять кадр.';
    haptic('select');
    // выбранная рамка должна быть видна над панелью, а не под ней
    requestAnimationFrame(() => revealSlot(name));
  }

  function closeSheet() {
    st.selected = null;
    if (st.doc) st.doc.querySelectorAll('.slot.sel').forEach((el) => el.classList.remove('sel'));
    $('sheet').classList.remove('open');
    $('sheet').setAttribute('aria-hidden', 'true');
    document.body.classList.remove('sheet-open');
    $('hint').textContent = 'Нажмите на любое фото в макете — снизу появятся снимки для замены.';
  }

  function revealSlot(name) {
    const el = slotEl(name);
    if (!el) return;
    const r = el.getBoundingClientRect();              // координаты внутри макета
    const frameTop = $('frameWrap').getBoundingClientRect().top + window.scrollY;
    const top = frameTop + r.top * st.scale;
    const height = r.height * st.scale;
    const visible = window.innerHeight - $('sheet').offsetHeight;
    const target = top - Math.max(12, (visible - height) / 2);
    window.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  }

  function buildStrip() {
    st.data.photos.forEach((p) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.idx = p.idx;
      b.setAttribute('aria-label', 'Снимок ' + (p.idx + 1));
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = '';
      img.src = p.thumb;
      b.appendChild(img);
      b.onclick = () => choosePhoto(p.idx);
      $('strip').appendChild(b);
    });
  }

  function markStrip(scrollToCurrent) {
    const s = st.selected && st.layout[st.selected];
    let current = null;
    document.querySelectorAll('#strip button').forEach((b) => {
      const on = !!s && s.photo === +b.dataset.idx;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on) current = b;
    });
    if (scrollToCurrent && current) {
      current.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
    }
  }

  function choosePhoto(idx) {
    const s = st.selected && st.layout[st.selected];
    if (!s) return;
    // новый снимок — сдвиг и масштаб от старого ему не подходят
    Object.assign(s, { photo: idx, zoom: 1, x: 50, y: 50 });
    paint(slotEl(st.selected), s);
    $('zoom').value = 1;
    paintZoom();
    markStrip(false);
    haptic('select');
  }

  function paintZoom() {
    const z = $('zoom');
    z.style.setProperty('--fill', ((z.value - z.min) / (z.max - z.min) * 100) + '%');
  }

  $('zoom').addEventListener('input', (e) => {
    const s = st.selected && st.layout[st.selected];
    if (!s) return;
    s.zoom = +e.target.value;
    paintZoom();
    paint(slotEl(st.selected), s);
  });

  $('resetBtn').addEventListener('click', () => {
    const s = st.selected && st.layout[st.selected];
    if (!s) return;
    Object.assign(s, { zoom: 1, x: 50, y: 50 });
    paint(slotEl(st.selected), s);
    $('zoom').value = 1;
    paintZoom();
  });

  $('doneBtn').addEventListener('click', closeSheet);

  // ── Стрелки ленты: видны, только когда в ту сторону есть что листать ─────
  function updateArrows() {
    const s = $('strip');
    const max = s.scrollWidth - s.clientWidth;
    $('stripWrap').classList.toggle('can-prev', s.scrollLeft > 4);
    $('stripWrap').classList.toggle('can-next', s.scrollLeft < max - 4);
  }
  function page(dir) {
    const s = $('strip');
    s.scrollBy({ left: dir * s.clientWidth * 0.8, behavior: 'smooth' });
  }
  $('prevBtn').addEventListener('click', () => page(-1));
  $('nextBtn').addEventListener('click', () => page(1));
  $('strip').addEventListener('scroll', updateArrows, { passive: true });
  window.addEventListener('resize', updateArrows);

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
      btn.textContent = 'Готово — файлы придут в чат';
      haptic('success');
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
