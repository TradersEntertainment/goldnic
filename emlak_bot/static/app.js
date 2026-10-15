/* Gold Nicomedia OS — istemci tarafı.
 * CSP "script-src 'self'" olduğu için satır içi <script>/onclick kullanılmaz;
 * tüm davranışlar data-* öznitelikleriyle buradan bağlanır. */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  // Türkçe duyarsız arama için sadeleştirme (sunucudaki normalize_title ile aynı mantık)
  const TR = { 'ç': 'c', 'ğ': 'g', 'ı': 'i', 'i̇': 'i', 'ö': 'o', 'ş': 's', 'ü': 'u', 'â': 'a', 'î': 'i', 'û': 'u' };
  const norm = (s) => (s || '').toLocaleLowerCase('tr-TR').replace(/i̇|[çğıöşüâîû]/g, (c) => TR[c] || c);

  const localISO = (d) => {
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  // ------------------------------------------------------------ servis çalışanı (PWA)
  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    });
  }

  // ------------------------------------------------------------ diyaloglar / alt sayfalar
  function openDialog(id, opener) {
    const d = document.getElementById(id);
    if (!d) return;
    let fill = null;
    if (opener && opener.dataset.fill) {
      try { fill = JSON.parse(opener.dataset.fill); } catch (_) { fill = null; }
    }
    if (fill) {
      if (fill.action) {
        const f = $('form[data-fill-form]', d) || $('form', d);
        if (f) f.action = fill.action;
      }
      Object.entries(fill.actions || {}).forEach(([sel, val]) => { const el = $(sel, d); if (el) el.action = val; });
      Object.entries(fill.text || {}).forEach(([sel, val]) => { const el = $(sel, d); if (el) el.textContent = val; });
      Object.entries(fill.value || {}).forEach(([sel, val]) => { const el = $(sel, d); if (el) el.value = val; });
    }
    if (typeof d.showModal === 'function') { if (!d.open) d.showModal(); } else d.setAttribute('open', '');
    const af = $('[autofocus]', d);
    if (af) setTimeout(() => af.focus(), 30);
  }

  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-open]');
    if (opener) { e.preventDefault(); openDialog(opener.dataset.open, opener); return; }
    const closer = e.target.closest('[data-close]');
    if (closer) {
      const d = closer.closest('dialog');
      if (d) { e.preventDefault(); d.close(); }
      return;
    }
    if (e.target.tagName === 'DIALOG') e.target.close(); // arka plana tıklayınca kapan
    const fx = e.target.closest('.flash-x');
    if (fx) { const f = fx.closest('.flash'); if (f) f.remove(); }
    const cp = e.target.closest('[data-copy]');
    if (cp) {
      const src = $(cp.dataset.copy);
      const text = src ? (src.value || src.textContent || '').trim() : '';
      if (text && navigator.clipboard) {
        navigator.clipboard.writeText(text).then(() => {
          const old = cp.textContent; cp.textContent = 'Kopyalandı ✓';
          setTimeout(() => { cp.textContent = old; }, 1600);
        }).catch(() => {});
      }
    }
  });

  // ------------------------------------------------------------ sekmeler
  document.addEventListener('click', (e) => {
    const tab = e.target.closest('[data-tab]');
    if (!tab) return;
    const box = tab.closest('[data-tabs]');
    if (!box) return;
    e.preventDefault();
    const key = tab.dataset.tab;
    $$('[data-tab]', box).forEach((t) => t.classList.toggle('active', t === tab));
    $$('[data-tab-panel]', box).forEach((p) => { p.hidden = p.dataset.tabPanel !== key; });
    const panel = $(`[data-tab-panel="${key}"]`, box);
    const focus = panel && $('[data-tab-focus]', panel);
    if (focus) setTimeout(() => focus.focus(), 60);
    if (box.dataset.tabs === 'hash') history.replaceState(null, '', `#${key}`);
  });
  $$('[data-tabs="hash"]').forEach((box) => {
    const key = location.hash.slice(1);
    const t = key && $(`[data-tab="${CSS.escape(key)}"]`, box);
    if (t) t.click();
  });

  // ------------------------------------------------------------ onay & otomatik gönderim
  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) { e.preventDefault(); return; }
    if (f.hasAttribute('data-reload-after')) setTimeout(() => window.location.reload(), 2500); // yeni belge listede görünsün
    if (f.target === '_blank' || f.hasAttribute('data-no-lock')) return;
    const btn = f.querySelector('button[type="submit"],button:not([type])');
    if (btn) setTimeout(() => { btn.disabled = true; btn.dataset.locked = '1'; }, 0); // çift gönderimi engelle
  }, true);
  window.addEventListener('pageshow', () => {
    $$('button[data-locked]').forEach((b) => { b.disabled = false; delete b.dataset.locked; });
  });
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.matches('[data-autosubmit]') && el.form) {
      if (typeof el.form.requestSubmit === 'function') el.form.requestSubmit(); else el.form.submit();
    }
  });

  // ------------------------------------------------------------ resim yedeği
  document.addEventListener('error', (e) => {
    const t = e.target;
    if (t && t.tagName === 'IMG' && t.hasAttribute('data-fallback')) {
      t.remove();
    }
  }, true);

  // ------------------------------------------------------------ tarih varsayılanları (yerel saat)
  $$('input[type="date"][data-default]').forEach((inp) => {
    if (inp.value) return;
    const d = new Date();
    if (inp.dataset.default === 'next-year') d.setFullYear(d.getFullYear() + 1);
    inp.value = localISO(d);
  });
  $$('input[type="date"][data-plus-year-from]').forEach((target) => {
    const src = $(target.dataset.plusYearFrom);
    if (!src) return;
    src.addEventListener('change', () => {
      if (!src.value) return;
      const d = new Date(`${src.value}T12:00:00`);
      d.setFullYear(d.getFullYear() + 1);
      target.value = localISO(d);
    });
  });

  // ------------------------------------------------------------ anlık tablo/kart filtresi
  $$('input[data-filter]').forEach((inp) => {
    const scope = $(inp.dataset.filter);
    if (!scope) return;
    const rows = $$('[data-search]', scope);
    const counter = inp.dataset.counter ? $(inp.dataset.counter) : null;
    inp.addEventListener('input', () => {
      const q = norm(inp.value.trim());
      let shown = 0;
      rows.forEach((r) => {
        const ok = !q || norm(r.dataset.search).includes(q);
        r.hidden = !ok;
        if (ok) shown += 1;
      });
      if (counter) counter.textContent = String(shown);
    });
  });

  // ------------------------------------------------------------ platforma göre kısayol etiketi
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || '') || /Mac OS/.test(navigator.userAgent);
  $$('[data-kbd]').forEach((k) => { k.textContent = isMac ? '⌘ K' : 'Ctrl K'; });

  // ------------------------------------------------------------ uygulamayı yükle (PWA)
  let deferredPrompt = null;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    $$('[data-install]').forEach((b) => { b.hidden = false; });
  });
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-install]');
    if (!b || !deferredPrompt) return;
    deferredPrompt.prompt();
    try { await deferredPrompt.userChoice; } catch (_) { /* yok say */ }
    deferredPrompt = null;
    b.hidden = true;
  });
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  if (standalone) document.documentElement.classList.add('is-standalone');
  if (/iphone|ipad|ipod/i.test(navigator.userAgent) && !standalone) {
    $$('.install-hint.ios').forEach((h) => h.classList.add('show'));
  }

  // ------------------------------------------------------------ PDF paylaş (WhatsApp vb. paylaşım menüsü)
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-share-pdf]');
    if (!b) return;
    e.preventDefault();
    const url = b.dataset.sharePdf;
    const name = b.dataset.filename || 'belge.pdf';
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(String(res.status));
      const blob = await res.blob();
      const file = new File([blob], name, { type: 'application/pdf' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: name, text: b.dataset.text || name });
        return;
      }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      window.location.href = url;
    }
  });

  // ------------------------------------------------------------ WhatsApp'tan gönder + "gönderildi" kaydı
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-wa-send]');
    if (!b) return;
    const markUrl = b.dataset.waSend;
    if (!markUrl) return;
    const body = new FormData();
    body.append('csrf', csrf());
    body.append('ajax', '1');
    fetch(markUrl, { method: 'POST', body, credentials: 'same-origin' })
      .then(() => {
        const card = b.closest('[data-match-card]');
        if (card) card.classList.add('sent');
        const label = card && $('[data-sent-label]', card);
        if (label) label.hidden = false;
      })
      .catch(() => {});
    // bağlantı (href) normal şekilde yeni sekmede açılır
  });

  // ------------------------------------------------------------ Talep havuzu: akıllı ayrıştırma önizlemesi
  $$('textarea[data-demand-parse]').forEach((ta) => {
    const box = $(ta.dataset.demandParse);
    const nameEl = ta.dataset.fillName ? $(ta.dataset.fillName) : null;
    const phoneEl = ta.dataset.fillPhone ? $(ta.dataset.fillPhone) : null;
    let t = null;
    let ctrl = null;
    const autoFilled = { name: false, phone: false };
    const run = () => {
      const q = ta.value.trim();
      if (!box) return;
      if (q.length < 3) { box.textContent = ''; return; }
      if (ctrl) ctrl.abort();
      ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      fetch(`/api/talepler/ayristir?q=${encodeURIComponent(q)}`, {
        credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctrl ? ctrl.signal : undefined,
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (!data) return;
          box.textContent = '';
          (data.chips || []).forEach((c) => {
            const b = document.createElement('span');
            b.className = `badge ${c.cls || ''}`;
            const NS = 'http://www.w3.org/2000/svg';
            const svg = document.createElementNS(NS, 'svg');
            svg.setAttribute('class', 'ic');
            const use = document.createElementNS(NS, 'use');
            use.setAttribute('href', `#i-${c.icon}`);
            svg.appendChild(use);
            b.appendChild(svg);
            b.appendChild(document.createTextNode(c.text));
            box.appendChild(b);
          });
          if (data.vague) {
            const s = document.createElement('span');
            s.className = 'mute';
            s.textContent = 'İpucu: oda, bütçe veya bölge yazarsanız eşleştirme keskinleşir.';
            box.appendChild(s);
          }
          // ad / telefon kutusu boşsa (veya daha önce otomatik dolduysa) metinden doldur
          if (nameEl && data.name && (!nameEl.value || autoFilled.name)) { nameEl.value = data.name; autoFilled.name = true; }
          if (phoneEl && data.phone && (!phoneEl.value || autoFilled.phone)) { phoneEl.value = data.phone; autoFilled.phone = true; }
        })
        .catch(() => {});
    };
    if (nameEl) nameEl.addEventListener('input', () => { autoFilled.name = false; });
    if (phoneEl) phoneEl.addEventListener('input', () => { autoFilled.phone = false; });
    ta.addEventListener('input', () => { clearTimeout(t); t = setTimeout(run, 220); });
    if (ta.value.trim()) run();
  });

  // Ctrl/⌘+Enter (veya data-enter-submit ile yalnız Enter; Shift+Enter yeni satır) formu gönderir
  document.addEventListener('keydown', (e) => {
    const ta = e.target;
    if (!ta || ta.tagName !== 'TEXTAREA' || e.key !== 'Enter' || e.isComposing) return;
    const quick = ta.hasAttribute('data-enter-submit') && !e.shiftKey;
    const ctrlEnter = ta.hasAttribute('data-ctrl-enter') && (e.ctrlKey || e.metaKey);
    if (!quick && !ctrlEnter) return;
    e.preventDefault();
    if (ta.form) { if (typeof ta.form.requestSubmit === 'function') ta.form.requestSubmit(); else ta.form.submit(); }
  });

  // Öneri çipine tıklayınca değeri hedef kutuya ekle ("Yahya Kaptan, Alikahya")
  document.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-append-to]');
    if (!chip) return;
    e.preventDefault();
    const inp = $(chip.dataset.appendTo);
    if (!inp) return;
    const val = chip.dataset.value || chip.textContent.trim();
    const parts = inp.value.split(',').map((x) => x.trim()).filter(Boolean);
    if (!parts.some((p) => norm(p) === norm(val))) parts.push(val);
    inp.value = parts.join(', ');
    inp.dispatchEvent(new Event('input', { bubbles: true }));
  });

  // <details> açma bağlantıları ve adres çubuğundaki #çapa ile açılış
  const openDetails = (sel) => {
    const d = sel && $(sel);
    if (d && d.tagName === 'DETAILS') { d.open = true; setTimeout(() => d.scrollIntoView({ behavior: 'smooth', block: 'start' }), 30); }
  };
  document.addEventListener('click', (e) => {
    const a = e.target.closest('[data-open-details]');
    if (a) { e.preventDefault(); openDetails(a.dataset.openDetails); history.replaceState(null, '', a.dataset.openDetails); }
    // açık eşleşme menüsünü dışarı tıklayınca kapat
    $$('details.mc-more[open]').forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });
  if (location.hash.length > 1) {
    try { openDetails(`details${location.hash}`); } catch (_) { /* geçersiz seçici */ }
  }

  // ------------------------------------------------------------ denetim izi: arama / WhatsApp tıklamaları
  document.addEventListener('click', (e) => {
    const a = e.target.closest('a[href^="tel:"], a[href^="https://wa.me/"]');
    if (!a || !navigator.sendBeacon) return;
    const tel = a.getAttribute('href').startsWith('tel:');
    const raw = tel ? a.getAttribute('href').slice(4) : (a.getAttribute('href').split('/')[3] || '').split('?')[0];
    const fd = new FormData();
    fd.append('csrf', csrf());
    fd.append('kind', tel ? 'tel' : 'wa');
    fd.append('target', raw);
    fd.append('page', location.pathname);
    try { navigator.sendBeacon('/api/iz', fd); } catch (_) { /* denetim kaydı işi durdurmamalı */ }
  });

  // ------------------------------------------------------------ Spotlight (Ctrl/⌘ + K)
  const spot = $('#spotlight');
  if (spot) {
    const input = $('#spotInput', spot);
    const box = $('#spotResults', spot);
    let items = [];
    let sel = 0;
    let timer = null;
    let ctrl = null;
    let lastFocus = null;

    const svgIcon = (name) => {
      const NS = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(NS, 'svg');
      svg.setAttribute('class', 'ic');
      svg.setAttribute('aria-hidden', 'true');
      const use = document.createElementNS(NS, 'use');
      use.setAttribute('href', `#i-${name || 'arrow-right'}`);
      svg.appendChild(use);
      return svg;
    };

    const highlight = () => {
      items.forEach((el, i) => {
        el.classList.toggle('sel', i === sel);
        el.setAttribute('aria-selected', i === sel ? 'true' : 'false');
      });
      if (items[sel]) items[sel].scrollIntoView({ block: 'nearest' });
    };

    const render = (data) => {
      box.textContent = '';
      items = [];
      sel = 0;
      const groups = (data && data.groups) || [];
      groups.forEach((g) => {
        if (!g.items || !g.items.length) return;
        const h = document.createElement('div');
        h.className = 'spot-group';
        h.textContent = g.title;
        box.appendChild(h);
        g.items.forEach((it) => {
          const a = document.createElement('a');
          a.className = 'spot-item';
          a.href = it.url;
          a.setAttribute('role', 'option');
          const ic = document.createElement('span');
          ic.className = 's-ic';
          ic.appendChild(svgIcon(it.icon));
          const body = document.createElement('span');
          body.className = 's-body';
          const t = document.createElement('div');
          t.className = 's-title';
          t.textContent = it.title;
          body.appendChild(t);
          if (it.subtitle) {
            const s = document.createElement('div');
            s.className = 's-sub';
            s.textContent = it.subtitle;
            body.appendChild(s);
          }
          a.appendChild(ic);
          a.appendChild(body);
          if (it.badge) {
            const b = document.createElement('span');
            b.className = `badge s-badge ${it.badge_class || ''}`;
            b.textContent = it.badge;
            a.appendChild(b);
          }
          a.addEventListener('mousemove', () => { const i = items.indexOf(a); if (i !== sel) { sel = i; highlight(); } });
          box.appendChild(a);
          items.push(a);
        });
      });
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'spot-empty';
        empty.textContent = input.value.trim()
          ? `“${input.value.trim()}” için sonuç bulunamadı.`
          : 'Aramaya başlayın: kiracı, ev sahibi, telefon, daire veya ilan.';
        box.appendChild(empty);
      }
      highlight();
    };

    const search = (q) => {
      if (ctrl) ctrl.abort();
      ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      fetch(`/api/spotlight?q=${encodeURIComponent(q)}`, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        signal: ctrl ? ctrl.signal : undefined,
      })
        .then((r) => (r.ok ? r.json() : { groups: [] }))
        .then(render)
        .catch((err) => { if (!err || err.name !== 'AbortError') render({ groups: [] }); });
    };

    const open = () => {
      if (!spot.hidden) return;
      lastFocus = document.activeElement;
      spot.hidden = false;
      document.body.style.overflow = 'hidden';
      input.value = '';
      search('');
      setTimeout(() => input.focus(), 10);
    };
    const close = () => {
      if (spot.hidden) return;
      spot.hidden = true;
      document.body.style.overflow = '';
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    };

    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => search(input.value.trim()), 110);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) { sel = (sel + 1) % items.length; highlight(); } }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) { sel = (sel - 1 + items.length) % items.length; highlight(); } }
      else if (e.key === 'Enter') { e.preventDefault(); if (items[sel]) window.location.href = items[sel].href; }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    spot.addEventListener('click', (e) => { if (e.target.closest('[data-spot-close]')) close(); });

    document.addEventListener('keydown', (e) => {
      const k = (e.key || '').toLowerCase();
      if ((e.ctrlKey || e.metaKey) && k === 'k') { e.preventDefault(); if (spot.hidden) open(); else close(); return; }
      if (k === 'escape' && !spot.hidden) { close(); return; }
      const tag = (e.target && e.target.tagName) || '';
      const typing = /INPUT|TEXTAREA|SELECT/.test(tag) || (e.target && e.target.isContentEditable);
      if (!typing && e.key === '/' && spot.hidden) { e.preventDefault(); open(); }
    });
    document.addEventListener('click', (e) => {
      if (e.target.closest('[data-spotlight]')) { e.preventDefault(); open(); }
    });
  }
})();
