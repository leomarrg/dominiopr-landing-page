/*
 * GA4 conversion events (generate_lead, begin_checkout, purchase).
 *
 * Loaded by _tracking.html only when GA is configured. An event is sent ONLY
 * when the visitor accepted analytics cookies (same localStorage key as the
 * consent banner) and gtag.js was loaded by dominioLoadGA(). Without consent
 * nothing is queued or sent. No personal data ever goes in the parameters:
 * only plan id/name, amounts, currency and our own order reference.
 *
 * Markup contract:
 *   <div data-ga-event="generate_lead" hidden></div>      fired on page load
 *   <div data-ga-event="purchase" data-ga-transaction="DOM-2026-0007"
 *        data-ga-value="1490.00" data-ga-tax="0" data-ga-currency="USD"
 *        data-ga-item-id="starter" data-ga-item-name="Starter"></div>
 *   <form data-ga-submit="begin_checkout"> ... <select name="plan"> with
 *        options carrying data-ga-value (fired on submit)
 * A page-load event the visitor had not consented to yet is sent if they
 * click "Aceptar" on the banner while still on the page.
 */
(function () {
  'use strict';
  var CONSENT_KEY = 'dominio_cookie_consent';
  var SENT_PREFIX = 'dominio_ga_sent:';

  function consented() {
    try { return localStorage.getItem(CONSENT_KEY) === 'accepted'; } catch (e) { return false; }
  }

  function track(name, params) {
    if (!consented() || !window.__dominioGA || typeof window.gtag !== 'function') return false;
    var p = params || {};
    p.transport_type = 'beacon';   // survives the navigation that follows a submit
    window.gtag('event', name, p);
    return true;
  }
  window.dominioTrack = track;

  function num(value) {
    var n = parseFloat(String(value || '').replace(/,/g, ''));
    return isFinite(n) ? n : 0;
  }

  function paramsFor(el) {
    var d = el.dataset;
    var params = {};
    if (d.gaCurrency) params.currency = d.gaCurrency;
    if (d.gaValue) params.value = num(d.gaValue);
    if (d.gaTax) params.tax = num(d.gaTax);
    if (d.gaTransaction) params.transaction_id = d.gaTransaction;
    if (d.gaItemId) {
      params.items = [{ item_id: d.gaItemId, item_name: d.gaItemName || d.gaItemId,
                        price: params.value || 0, quantity: 1 }];
    }
    return params;
  }

  // A purchase must count once even if the welcome page is reloaded.
  function alreadySent(el) {
    var tx = el.dataset.gaTransaction;
    if (!tx) return false;
    try { return localStorage.getItem(SENT_PREFIX + tx) === '1'; } catch (e) { return false; }
  }
  function markSent(el) {
    var tx = el.dataset.gaTransaction;
    if (!tx) return;
    try { localStorage.setItem(SENT_PREFIX + tx, '1'); } catch (e) {}
  }

  function firePageEvents() {
    var pending = 0;
    document.querySelectorAll('[data-ga-event]').forEach(function (el) {
      if (el.dataset.gaDone === '1' || alreadySent(el)) return;
      if (track(el.dataset.gaEvent, paramsFor(el))) {
        el.dataset.gaDone = '1';
        markSent(el);
      } else {
        pending++;
      }
    });
    return pending;
  }

  if (firePageEvents()) {
    // Consent given later on this same page: the banner's own click handler
    // (registered first) stores the choice and loads gtag.js, then this runs.
    var accept = document.getElementById('cookieAccept');
    if (accept) accept.addEventListener('click', function () { firePageEvents(); });
  }

  document.querySelectorAll('form[data-ga-submit]').forEach(function (form) {
    form.addEventListener('submit', function () {
      var plan = form.querySelector('[name="plan"]');
      var option = plan && plan.options ? plan.options[plan.selectedIndex] : null;
      // Only a submit that can reach checkout: a plan picked and the
      // required fields filled (the server re-validates everything).
      if (!option || !option.value) return;
      var required = form.querySelectorAll('[required]');
      for (var i = 0; i < required.length; i++) {
        if (!String(required[i].value || '').trim()) return;
      }
      var period = form.querySelector('[name="period"]');
      var annual = period && period.value === 'annual';
      var value = num(annual ? option.dataset.gaAnnual : option.dataset.gaMonthly);
      var params = { items: [{ item_id: option.value,
                               item_name: option.dataset.gaName || option.value,
                               item_variant: annual ? 'annual' : 'monthly',
                               price: value, quantity: 1 }] };
      if (value) { params.value = value; params.currency = 'USD'; }
      track(form.dataset.gaSubmit, params);
    });
  });
})();
