/* DOMINIO embeddable AI agent — self-contained widget for {{ name|escapejs }}. */
(function () {
    'use strict';
    if (window.__dmnAgentLoaded) return;
    window.__dmnAgentLoaded = true;

    var CFG = {
        slug: '{{ slug|escapejs }}',
        name: '{{ name|escapejs }}',
        greeting: '{{ greeting|escapejs }}',
        color: '{{ color|escapejs }}',
        colorRgb: '{{ color_rgb|escapejs }}',
        onColor: '{{ on_color|escapejs }}',
        panel: '{{ panel|escapejs }}',
        surfaceAlt: '{{ surface_alt|escapejs }}',
        text: '{{ text|escapejs }}',
        muted: '{{ muted|escapejs }}',
        bubbleAText: '{{ bubble_a_text|escapejs }}',
        headerBg: '{{ header_bg|escapejs }}',
        headerText: '{{ header_text|escapejs }}',
        fabBg: '{{ fab_bg|escapejs }}',
        fabText: '{{ fab_text|escapejs }}',
        border: '{{ border|escapejs }}',
        borderSoft: '{{ border_soft|escapejs }}',
        borderBubble: '{{ border_bubble|escapejs }}',
        api: '{{ api_url|escapejs }}'
    };

    var messages = [], greeted = false, busy = false;

    // ---- styles (scoped under .dmn-* so the host page is untouched) ----
    var css = ''
        + '.dmn-fab{position:fixed;right:20px;bottom:20px;z-index:2147483000;display:inline-flex;'
        + 'align-items:center;gap:9px;padding:12px 18px;border:none;border-radius:999px;cursor:pointer;'
        + 'background:' + CFG.fabBg + ';color:' + CFG.fabText + ';font-family:system-ui,sans-serif;font-weight:600;'
        + 'font-size:15px;box-shadow:0 6px 18px rgba(0,0,0,.28)}'
        + '.dmn-fab svg{width:20px;height:20px}'
        + '.dmn-open .dmn-fab{display:none}'
        + '.dmn-panel{position:fixed;right:20px;bottom:20px;z-index:2147483000;width:370px;max-width:calc(100vw - 32px);'
        + 'height:560px;max-height:calc(100vh - 40px);display:none;flex-direction:column;overflow:hidden;'
        + 'border-radius:18px;background:' + CFG.panel + ';border:1px solid ' + CFG.border + ';'
        + 'box-shadow:0 24px 60px rgba(0,0,0,.45);font-family:system-ui,sans-serif}'
        + '.dmn-open .dmn-panel{display:flex}'
        + '.dmn-head{display:flex;align-items:center;justify-content:space-between;padding:15px 18px;'
        + 'background:' + CFG.headerBg + ';border-bottom:1px solid ' + CFG.borderSoft + '}'
        + '.dmn-head b{color:' + CFG.headerText + ';font-size:15px;display:block}'
        + '.dmn-head small{color:' + CFG.headerText + ';opacity:.6;font-size:12px}'
        + '.dmn-x{background:none;border:none;color:' + CFG.headerText + ';opacity:.6;font-size:24px;line-height:1;cursor:pointer}'
        + '.dmn-log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}'
        + '.dmn-b{max-width:82%;padding:10px 13px;border-radius:14px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word}'
        + '.dmn-a{align-self:flex-start;background:' + CFG.surfaceAlt + ';color:' + CFG.bubbleAText + ';border:1px solid ' + CFG.borderBubble + ';border-bottom-left-radius:4px}'
        + '.dmn-u{align-self:flex-end;background:' + CFG.color + ';color:' + CFG.onColor + ';font-weight:500;border-bottom-right-radius:4px}'
        + '.dmn-quick{display:flex;flex-wrap:wrap;gap:7px}'
        + '.dmn-chip{padding:8px 13px;border-radius:999px;border:1px solid ' + CFG.color + ';background:transparent;color:' + CFG.color + ';font:inherit;font-size:13px;cursor:pointer}'
        + '.dmn-form{display:flex;gap:8px;padding:12px 14px 6px;border-top:1px solid ' + CFG.borderSoft + '}'
        + '.dmn-input{flex:1;min-width:0;padding:11px 14px;border-radius:999px;border:1px solid ' + CFG.borderBubble + ';background:' + CFG.surfaceAlt + ';color:' + CFG.text + ';font:inherit;font-size:16px}'
        + '.dmn-input::placeholder{color:' + CFG.muted + '}'
        + '.dmn-send{flex:0 0 auto;width:42px;height:42px;border:none;border-radius:50%;background:' + CFG.color + ';color:' + CFG.onColor + ';font-size:16px;cursor:pointer}'
        + '.dmn-foot{margin:0;padding:4px 14px 12px;font-size:11px;color:' + CFG.muted + ';text-align:center}'
        + '.dmn-typing span{display:inline-block;width:6px;height:6px;border-radius:50%;background:' + CFG.color + ';opacity:.5;margin:0 2px;animation:dmnT 1s infinite}'
        + '.dmn-typing span:nth-child(2){animation-delay:.15s}.dmn-typing span:nth-child(3){animation-delay:.3s}'
        + '@keyframes dmnT{0%,60%,100%{transform:translateY(0);opacity:.4}30%{transform:translateY(-4px);opacity:1}}';
    var style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    // ---- DOM ----
    var root = document.createElement('div');
    root.className = 'dmn-root';
    root.innerHTML =
        '<button class="dmn-fab" type="button" aria-label="Chat">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.6-.8L3 21l1.9-5.4A8.38 8.38 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.38 8.38 0 0 1 21 11.5z"/></svg>'
        + '<span>Chat</span></button>'
        + '<div class="dmn-panel" role="dialog" aria-label="Chat">'
        + '<div class="dmn-head"><div><b></b><small>AI assistant</small></div><button class="dmn-x" aria-label="Close">&times;</button></div>'
        + '<div class="dmn-log"></div>'
        + '<form class="dmn-form"><input class="dmn-input" type="text" maxlength="2000" placeholder="Type your question..." autocomplete="off"><button class="dmn-send" type="submit" aria-label="Send">&#10148;</button></form>'
        + '<p class="dmn-foot">AI assistant</p></div>';
    document.body.appendChild(root);
    root.querySelector('.dmn-head b').textContent = CFG.name;

    var fab = root.querySelector('.dmn-fab'),
        panel = root.querySelector('.dmn-panel'),
        log = root.querySelector('.dmn-log'),
        form = root.querySelector('.dmn-form'),
        input = root.querySelector('.dmn-input'),
        sendBtn = root.querySelector('.dmn-send'),
        xBtn = root.querySelector('.dmn-x');

    function bubble(role, text) {
        var d = document.createElement('div');
        d.className = 'dmn-b ' + (role === 'user' ? 'dmn-u' : 'dmn-a');
        d.textContent = text;
        log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
    }
    function open() {
        root.classList.add('dmn-open');
        if (!greeted) { bubble('assistant', CFG.greeting); greeted = true; }
        setTimeout(function () { input.focus(); }, 120);
    }
    function close() { root.classList.remove('dmn-open'); }
    function typing(on) {
        var t = root.querySelector('.dmn-typing');
        if (on && !t) { t = document.createElement('div'); t.className = 'dmn-b dmn-a dmn-typing'; t.innerHTML = '<span></span><span></span><span></span>'; log.appendChild(t); log.scrollTop = log.scrollHeight; }
        else if (!on && t) { t.remove(); }
    }
    function setBusy(b) { busy = b; input.disabled = b; sendBtn.disabled = b; }

    function send(text) {
        bubble('user', text); messages.push({ role: 'user', content: text });
        setBusy(true); typing(true);
        fetch(CFG.api, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client: CFG.slug, messages: messages })
        }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (x) {
                typing(false);
                if (x.ok && x.d.reply) { bubble('assistant', x.d.reply); messages.push({ role: 'assistant', content: x.d.reply }); }
                else { bubble('assistant', x.d.error || 'Sorry, something went wrong.'); }
            })
            .catch(function () { typing(false); bubble('assistant', 'Connection issue — please try again.'); })
            .then(function () { setBusy(false); input.focus(); });
    }

    fab.addEventListener('click', function () { root.classList.contains('dmn-open') ? close() : open(); });
    xBtn.addEventListener('click', close);
    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var t = input.value.trim(); if (!t || busy) return;
        input.value = ''; send(t);
    });
})();
