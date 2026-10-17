/* DOMINIO dashboard behaviours (defer, no deps; progressive enhancement). Hooks: [data-drawer-open|close], [data-panel-open|close],
 * [data-autosubmit], [data-tip]/[data-chart], [data-kanban], .dash-row[data-target]. */
(function () {
  'use strict';
  var doc = document, root = doc.documentElement, body = doc.body;
  var $ = function (s, c) { return (c || doc).querySelector(s); };
  var $$ = function (s, c) { return [].slice.call((c || doc).querySelectorAll(s)); };
  var closest = function (el, s) { return el && el.closest ? el.closest(s) : null; };
  var FOCUS = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';
  var shown = function (el) { return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)); };
  var live;
  function announce(text) {
    if (!live) {
      live = doc.createElement('div');
      live.className = 'sr-only';
      live.setAttribute('role', 'status');
      body.appendChild(live);
    }
    live.textContent = '';
    setTimeout(function () { live.textContent = text; }, 30);
  }
  function notice(text) {
    var old = $('.dash-notice');
    if (old) old.remove();
    var n = doc.createElement('div');
    n.className = 'alert alert--danger dash-notice';
    n.setAttribute('role', 'alert');
    n.textContent = text;
    body.appendChild(n);
    clearTimeout(notice.t);
    notice.t = setTimeout(function () { n.remove(); }, 7000);
  }
  var wide = window.matchMedia('(min-width: 1024px)'), cur = null;
  function openOverlay(el, trigger, nav) {
    if (!el || cur) return;
    cur = { el: el, trigger: trigger, nav: nav, off: [], scrim: null };
    if (nav) {
      root.classList.add('is-drawer-open');
      el.setAttribute('role', 'dialog');
      el.setAttribute('aria-modal', 'true');
      el.setAttribute('aria-label', 'Menú');
    } else {
      cur.scrim = doc.createElement('div');
      cur.scrim.className = 'drawer-scrim';
      cur.scrim.addEventListener('click', function () { closeOverlay(); });
      body.appendChild(cur.scrim);
      el.classList.add('is-open');
      el.setAttribute('aria-hidden', 'false');
    }
    var keep = el;
    while (keep.parentNode && keep.parentNode !== body) keep = keep.parentNode;
    [].forEach.call(body.children, function (c) {
      if (c === keep || c === cur.scrim || c === live || c.tagName === 'SCRIPT' || /dash-(sprite|scrim)/.test(c.getAttribute('class') || '')) return;
      if (!c.inert) { c.inert = true; cur.off.push(c); }
    });
    $$('[data-drawer-open],[data-panel-open]').forEach(function (t) { t.setAttribute('aria-expanded', 'true'); });
    var first = $('[data-autofocus]', el) || $(FOCUS, el);
    if (first) first.focus({ preventScroll: true });
  }
  function closeOverlay(silent) {
    if (!cur) return;
    var c = cur;
    cur = null;
    if (c.nav) {
      root.classList.remove('is-drawer-open');
      ['role', 'aria-modal', 'aria-label'].forEach(function (a) { c.el.removeAttribute(a); });
    } else {
      c.el.classList.remove('is-open');
      c.el.setAttribute('aria-hidden', 'true');
      c.scrim.remove();
    }
    c.off.forEach(function (n) { n.inert = false; });
    $$('[data-drawer-open],[data-panel-open]').forEach(function (t) { t.setAttribute('aria-expanded', 'false'); });
    if (silent === true) return;
    var back = shown(c.trigger) ? c.trigger : $$('[data-drawer-open]').filter(shown)[0] || $('#dash-main');
    if (back) back.focus({ preventScroll: true });
  }
  doc.addEventListener('click', function (e) {
    var t = closest(e.target, '[data-drawer-open],[data-drawer-close],[data-panel-open],[data-panel-close]');
    if (!t) return;
    e.preventDefault();
    if (t.hasAttribute('data-drawer-open')) { if (!wide.matches) openOverlay($('#dash-sidebar'), t, true); }
    else if (t.hasAttribute('data-panel-open')) openOverlay($(t.getAttribute('data-panel-open')), t, false);
    else closeOverlay();
  });
  doc.addEventListener('keydown', function (e) {
    if (!cur) return;
    if (e.key === 'Escape') { e.preventDefault(); closeOverlay(); return; }
    if (e.key !== 'Tab') return;
    var items = $$(FOCUS, cur.el).filter(shown), first = items[0], last = items[items.length - 1];
    if (!first) { e.preventDefault(); return; }
    if (e.shiftKey && (doc.activeElement === first || !cur.el.contains(doc.activeElement))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  wide.addEventListener('change', function () {
    if (cur && cur.nav && wide.matches) closeOverlay(true);
  });
  var submit = function (f) { if (f.requestSubmit) f.requestSubmit(); else f.submit(); };
  doc.addEventListener('change', function (e) {
    var t = e.target, f = closest(t, 'form[data-autosubmit]');
    if (f && /^(SELECT|INPUT)$/.test(t.tagName)) submit(f);
    else if (t.tagName === 'SELECT' && t.hasAttribute('data-autosubmit') && t.form) submit(t.form);
  });
  var tip, tipFor, TIP = '.chart [data-tip],[data-chart] [data-tip]';
  function showTip(el, x, y) {
    var t = tip;
    if (!t) {
      t = tip = doc.createElement('div');
      t.className = 'dash-tooltip';
      t.setAttribute('role', 'tooltip');
      body.appendChild(t);
    }
    t.textContent = el.getAttribute('data-tip');
    t.hidden = false;
    if (x == null) { var r = el.getBoundingClientRect(); x = r.left + r.width / 2; y = r.top; }
    var w = t.offsetWidth, h = t.offsetHeight;
    t.style.left = Math.min(Math.max(8, x - w / 2), window.innerWidth - w - 8) + 'px';
    t.style.top = (y - h - 12 < 8 ? y + 20 : y - h - 12) + 'px';
    if (tipFor && tipFor !== el) tipFor.classList.remove('is-active');
    tipFor = el;
    el.classList.add('is-active');
    var c = closest(el, '.chart,[data-chart]');
    if (c) c.classList.add('has-active');
  }
  function hideTip() {
    if (tip) tip.hidden = true;
    if (!tipFor) return;
    tipFor.classList.remove('is-active');
    var c = closest(tipFor, '.chart,[data-chart]');
    if (c) c.classList.remove('has-active');
    tipFor = null;
  }
  doc.addEventListener('pointermove', function (e) {
    if (e.pointerType === 'touch') return;
    var el = closest(e.target, TIP);
    if (el) showTip(el, e.clientX, e.clientY); else if (tipFor) hideTip();
  });
  doc.addEventListener('pointerdown', function (e) {
    var el = closest(e.target, TIP);
    if (el && e.pointerType === 'touch') showTip(el, e.clientX, e.clientY); else if (!el) hideTip();
  });
  doc.addEventListener('focusin', function (e) { if (e.target.matches && e.target.matches(TIP)) showTip(e.target); });
  doc.addEventListener('focusout', function (e) { if (e.target.matches && e.target.matches(TIP)) hideTip(); });
  window.addEventListener('scroll', hideTip, { passive: true });
  $$('[data-chart]').forEach(function (box) {
    var items = $$('[data-tip]', box), i = -1;
    if (!items.length) return;
    if (!box.hasAttribute('tabindex')) box.setAttribute('tabindex', '0');
    function go(n) {
      i = (n + items.length) % items.length;
      showTip(items[i]);
      announce(items[i].getAttribute('data-tip').replace(/\n+/g, ', '));
    }
    box.addEventListener('keydown', function (e) {
      var k = e.key, last = items.length - 1;
      var next = k === 'ArrowRight' || k === 'ArrowDown' ? (i < 0 ? 0 : i + 1)
        : k === 'ArrowLeft' || k === 'ArrowUp' ? (i < 0 ? last : i - 1)
        : null;
      if (next !== null) { e.preventDefault(); go(next); }
      else if (k === 'Escape') { hideTip(); i = -1; }
    });
    box.addEventListener('blur', function () { hideTip(); i = -1; });
  });
  function csrf(scope) {
    var i = $('input[name=csrfmiddlewaretoken]', scope) || $('input[name=csrfmiddlewaretoken]'), m = $('meta[name=csrf-token]');
    return (i && i.value) || (m && m.content) || '';
  }
  function initKanban(board) {
    var cols = $$('.kanban-col', board), drag = null, justDragged = false;
    var errText = board.getAttribute('data-error') || 'No se pudo mover el lead. Se devolvió a su columna.';
    var okText = board.getAttribute('data-moved') || 'Lead movido a';
    var colOf = function (s) { return cols.filter(function (c) { return c.getAttribute('data-status') === s; })[0]; };
    // Column totals come from the server (a column can hold more leads than the cards shown): adjust them by the move, never recount cards.
    var bump = function (c, d) { var n = c && $('[data-count]', c); if (n) n.textContent = Math.max(0, (parseInt(n.textContent, 10) || 0) + d); };
    function post(card, status) {
      var form = $('form.lead-card__move', card), token = csrf(card), data = new URLSearchParams();
      var url = form ? form.getAttribute('action') : (board.getAttribute('data-move-url') || '').replace('__ID__', card.getAttribute('data-lead-id'));
      data.set('status', status);
      data.set('csrfmiddlewaretoken', token);
      return fetch(url, {
        method: 'POST', credentials: 'same-origin', body: data,
        headers: { 'Accept': 'application/json', 'X-CSRFToken': token, 'X-Requested-With': 'XMLHttpRequest' }
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        if ((r.headers.get('content-type') || '').indexOf('json') < 0) return {};
        return r.json().then(function (j) { if (j && j.ok === false) throw new Error(j.error); return j || {}; });
      });
    }
    function move(card, to) {
      var from = card.closest('.kanban-col');
      if (!to || to === from) return;
      var next = card.nextElementSibling, was = card.getAttribute('data-status'), status = to.getAttribute('data-status');
      var sel = $('form.lead-card__move select[name=status]', card), dest = $('.kanban-col__body', to);
      dest.insertBefore(card, $('.lead-card', dest));
      card.setAttribute('data-status', status);
      if (sel) sel.value = status;
      card.classList.add('is-pending');
      bump(from, -1);
      bump(to, 1);
      post(card, status).then(function (j) {
        card.classList.remove('is-pending');
        var age = $('[data-age]', card);
        if (age && j.age) age.textContent = j.age;
        announce(okText + ' ' + $('.kanban-col__title', to).textContent.trim());
      }).catch(function () {
        card.classList.remove('is-pending');
        $('.kanban-col__body', from).insertBefore(card, next && next.parentNode ? next : null);
        card.setAttribute('data-status', was);
        if (sel) sel.value = was;
        card.classList.add('is-failed');
        setTimeout(function () { card.classList.remove('is-failed'); }, 4000);
        bump(to, -1);
        bump(from, 1);
        notice(errText);
      });
    }
    board.addEventListener('submit', function (e) {
      var form = closest(e.target, 'form.lead-card__move'), col = form && form.elements.status && colOf(form.elements.status.value);
      if (!col) return; // unknown column: let the plain POST happen
      e.preventDefault();
      var card = form.closest('.lead-card');
      move(card, col);
      card.focus({ preventScroll: true });
    });
    function colAt(x, y) {
      var c = closest(doc.elementFromPoint(x, y), '.kanban-col');
      return c && board.contains(c) ? c : null;
    }
    function stop() {
      doc.removeEventListener('pointermove', onMove);
      doc.removeEventListener('pointerup', onUp);
      doc.removeEventListener('pointercancel', onCancel);
      doc.removeEventListener('keydown', onKey, true);
      if (!drag) return;
      if (drag.ghost) drag.ghost.remove();
      drag.card.classList.remove('is-dragging');
      cols.forEach(function (c) { c.classList.remove('is-over'); });
      root.classList.remove('is-dragging');
      drag = null;
    }
    function onMove(e) {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.ghost) {
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 8) return;
        var r = drag.card.getBoundingClientRect(), g = drag.card.cloneNode(true);
        drag.dx = drag.x - r.left;
        drag.dy = drag.y - r.top;
        g.className += ' lead-card--ghost';
        g.setAttribute('aria-hidden', 'true');
        $$('form,input,select,button,a', g).forEach(function (n) { n.setAttribute('tabindex', '-1'); });
        g.style.width = r.width + 'px';
        body.appendChild(g);
        drag.ghost = g;
        drag.card.classList.add('is-dragging');
        root.classList.add('is-dragging');
      }
      e.preventDefault();
      drag.ghost.style.left = (e.clientX - drag.dx) + 'px';
      drag.ghost.style.top = (e.clientY - drag.dy) + 'px';
      var over = colAt(e.clientX, e.clientY), b = board.getBoundingClientRect();
      cols.forEach(function (c) { c.classList.toggle('is-over', c === over); });
      if (e.clientX < b.left + 48) board.scrollLeft -= 14; else if (e.clientX > b.right - 48) board.scrollLeft += 14;
    }
    function onUp(e) {
      if (!drag || e.pointerId !== drag.id) return;
      var d = drag, to = d.ghost ? colAt(e.clientX, e.clientY) : null;
      stop();
      if (d.ghost) {
        justDragged = true;
        setTimeout(function () { justDragged = false; }, 60);
        move(d.card, to);
      }
    }
    function onCancel(e) { if (drag && e.pointerId === drag.id) stop(); }
    function onKey(e) { if (e.key === 'Escape' && drag) { e.stopPropagation(); stop(); } }
    board.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      var card = closest(e.target, '.lead-card'), grip = closest(e.target, '[data-drag-handle]');
      if (!card || (!grip && (e.pointerType !== 'mouse' || closest(e.target, 'a,button,select,input,textarea,label,summary')))) return;
      drag = { card: card, id: e.pointerId, x: e.clientX, y: e.clientY };
      if (e.pointerType === 'mouse') e.preventDefault();
      doc.addEventListener('pointermove', onMove, { passive: false });
      doc.addEventListener('pointerup', onUp);
      doc.addEventListener('pointercancel', onCancel);
      doc.addEventListener('keydown', onKey, true);
    });
    board.addEventListener('click', function (e) { if (justDragged) { e.preventDefault(); e.stopPropagation(); } }, true);
  }
  $$('[data-kanban]').forEach(initKanban);
  function toggleRow(row) {
    var d = doc.getElementById(row.getAttribute('data-target'));
    if (!d) return;
    var open = d.classList.toggle('is-open');
    row.classList.toggle('is-expanded', open);
    row.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  doc.addEventListener('click', function (e) {
    var t = e.target, row = closest(t, '.dash-row[data-target]'), btn = closest(t, '.dash-reply-toggle[data-reply]');
    if (row && !closest(t, 'a,button,select,input,textarea,label,summary,form,[data-stop-click]')) toggleRow(row);
    if (btn) { var f = doc.getElementById(btn.getAttribute('data-reply')); if (f) f.classList.toggle('is-open'); }
  });
  doc.addEventListener('keydown', function (e) {
    var t = e.target;
    if (t.matches && t.matches('.dash-row[data-target]') && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleRow(t); }
  });
  doc.addEventListener('submit', function (e) {
    var b = e.target.matches && e.target.matches('.dash-reply-form') && $('button[type=submit]', e.target);
    if (b) { b.disabled = true; b.textContent = 'Enviando…'; }
  });
})();
