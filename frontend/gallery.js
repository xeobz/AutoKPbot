// Сетка выбора фото. Порядок важен: первое выбранное фото несёт текст КП,
// поэтому на выбранных рисуем порядковый номер.
import { h } from './ui.js';
import { haptic } from './tg.js';

/** photos — все фото, selected — выбранные url по порядку, onChange(newSelected). */
export function photoGrid({ photos, selected, onChange }) {
  const grid = h('div', { class: 'gallery' });
  (photos || []).forEach((url, i) => {
    const pos = selected.indexOf(url);
    const on = pos >= 0;
    grid.appendChild(h('button', {
      class: 'photo',
      type: 'button',
      'aria-pressed': on ? 'true' : 'false',
      'aria-label': `Фото ${i + 1}${on ? `, выбрано ${pos + 1}-м` : ''}`,
      onclick: () => {
        haptic.select();
        const next = selected.slice();
        if (pos >= 0) next.splice(pos, 1); else next.push(url);
        onChange(next);
      },
    },
      h('img', { src: url, alt: '', loading: 'lazy' }),
      on ? h('span', { class: 'photo-badge', text: String(pos + 1) }) : null,
    ));
  });
  return grid;
}

/** Строка над галереей: счётчик выбранных + «Снять всё». */
export function galleryBar({ selectedCount, total, onClear }) {
  return h('div', { class: 'gallery-bar' },
    h('span', { class: 'hint nums', text: `Выбрано ${selectedCount} из ${total}` }),
    h('button', {
      class: 'btn btn-second btn-sm', type: 'button', text: 'Снять всё',
      disabled: selectedCount === 0, onclick: onClear,
    }),
  );
}
