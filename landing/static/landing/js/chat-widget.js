/* Ask DOMINIO — AI chat widget.
 * Talks to the Django /api/chat/ endpoint, which proxies to Claude.
 * The full conversation is kept client-side and sent each turn (the API is stateless).
 */
(function () {
    'use strict';

    var widget = document.getElementById('chatWidget');
    if (!widget) return;

    var fab = document.getElementById('chatFab');
    var panel = document.getElementById('chatPanel');
    var closeBtn = document.getElementById('chatClose');
    var log = document.getElementById('chatLog');
    var form = document.getElementById('chatForm');
    var input = document.getElementById('chatText');
    var sendBtn = document.getElementById('chatSend');

    var chatUrl = widget.getAttribute('data-chat-url');
    var csrfToken = widget.getAttribute('data-csrf');

    var messages = [];        // {role, content} sent to the API
    var greeted = false;
    var busy = false;
    var rated = false, ratingOffered = false, gotReply = false;

    // M-02: random session id so the server can persist this conversation.
    var SESSION = (function () {
        try {
            var a = new Uint8Array(16); crypto.getRandomValues(a);
            return Array.prototype.map.call(a, function (b) {
                return ('0' + b.toString(16)).slice(-2);
            }).join('');
        } catch (e) { return 's' + Date.now() + Math.floor(Math.random() * 1e9); }
    })();
    var surveyUrl = chatUrl.replace(/chat\/?$/, 'survey/');

    // A11y: keyboard users must not tab out of an open dialog (WCAG 2.1.1/2.4.3).
    // `role="log" aria-live="polite"` lives in the markup, not here.
    function trapFocus(e) {
        if (e.key !== 'Tab' || !widget.classList.contains('is-open')) return;
        var f = Array.prototype.filter.call(
            panel.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
            function (el) { return !el.disabled && el.offsetParent !== null; });
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    panel.addEventListener('keydown', trapFocus);

    var GREETING = "¡Hola, bienvenido a DOMINIO! ¿Qué quieres construir? " +
        "Escoge una opción para empezar, o escríbeme tu pregunta.";

    // Service quick-picks shown first, so visitors choose instead of typing.
    var SERVICE_OPTIONS = [
        { label: 'Plataforma a la medida', message: "Me interesa una plataforma operacional a la medida." },
        { label: 'Dashboards y data', message: "Me interesan los dashboards y sistemas de reportes." },
        { label: 'Agente de IA / automatización', message: "Me interesa un agente de IA o automatización." },
        { label: 'Landing page y formularios', message: "Necesito una landing page, formularios o un sistema de campañas." },
        { label: 'Estrategia y consultoría', message: "Me gustaría estrategia de producto o consultoría técnica." }
    ];

    function openPanel() {
        widget.classList.add('is-open');
        panel.setAttribute('aria-hidden', 'false');
        fab.setAttribute('aria-expanded', 'true');
        if (!greeted) {
            addBubble('assistant', GREETING);
            renderQuickReplies(SERVICE_OPTIONS);
            greeted = true;
        }
        setTimeout(function () { input.focus(); }, 150);
    }

    function renderQuickReplies(options) {
        var wrap = document.createElement('div');
        wrap.className = 'chat-quick';
        wrap.id = 'chatQuick';
        options.forEach(function (opt) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'chat-chip';
            b.textContent = opt.label;
            b.addEventListener('click', function () {
                wrap.remove();
                send(opt.message);
            });
            wrap.appendChild(b);
        });
        log.appendChild(wrap);
        log.scrollTop = log.scrollHeight;
    }

    function clearQuickReplies() {
        var q = document.getElementById('chatQuick');
        if (q) q.remove();
    }

    // M-18: one-tap CSAT before closing (once, only if the agent replied).
    function offerRating() {
        if (rated || ratingOffered || !gotReply) return false;
        ratingOffered = true;
        var wrap = document.createElement('div');
        wrap.className = 'chat-quick';
        var label = document.createElement('div');
        label.className = 'chat-bubble chat-bubble--assistant';
        label.textContent = '¿Te ayudé? Puntúame (opcional):';
        log.appendChild(label);
        for (var i = 1; i <= 5; i++) {
            (function (score) {
                var b = document.createElement('button');
                b.type = 'button';
                b.className = 'chat-chip';
                b.textContent = score + '★';
                b.setAttribute('aria-label', score + ' de 5');
                b.addEventListener('click', function () {
                    rated = true; wrap.remove(); label.remove();
                    fetch(surveyUrl, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                        body: JSON.stringify({ session: SESSION, score: score })
                    }).catch(function () {});
                    addBubble('assistant', '¡Gracias!');
                    setTimeout(closePanelNow, 700);
                });
                wrap.appendChild(b);
            })(i);
        }
        log.appendChild(wrap);
        log.scrollTop = log.scrollHeight;
        return true;
    }

    function closePanelNow() {
        // Move focus back to the launcher, otherwise it falls to <body> and the
        // keyboard user loses their place.
        var hadFocusInside = panel.contains(document.activeElement);
        widget.classList.remove('is-open');
        panel.setAttribute('aria-hidden', 'true');
        fab.setAttribute('aria-expanded', 'false');
        if (hadFocusInside) fab.focus();
    }

    function closePanel() {
        if (offerRating()) return;  // first close shows the optional rating
        closePanelNow();
    }

    // Escape HTML, then render a tiny, safe subset of markdown the model emits:
    // **bold**, *italic*, and [text](url) links. Newlines render via CSS pre-wrap.
    function renderRich(text) {
        var esc = String(text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        return esc
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
            .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)"'<>]+)\)/g,
                '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    }

    function addBubble(role, text) {
        var bubble = document.createElement('div');
        bubble.className = 'chat-bubble chat-bubble--' + role;
        if (role === 'assistant') bubble.innerHTML = renderRich(text);
        else bubble.textContent = text;
        log.appendChild(bubble);
        log.scrollTop = log.scrollHeight;
        return bubble;
    }

    function showTyping() {
        var t = document.createElement('div');
        t.className = 'chat-bubble chat-bubble--assistant chat-typing';
        t.id = 'chatTyping';
        t.innerHTML = '<span class="sr-only">Escribiendo…</span>' +
            '<span aria-hidden="true"></span><span aria-hidden="true"></span><span aria-hidden="true"></span>';
        log.appendChild(t);
        log.scrollTop = log.scrollHeight;
    }

    function hideTyping() {
        var t = document.getElementById('chatTyping');
        if (t) t.remove();
    }

    function setBusy(state) {
        busy = state;
        input.disabled = state;
        sendBtn.disabled = state;
    }

    function send(text) {
        clearQuickReplies();
        addBubble('user', text);
        messages.push({ role: 'user', content: text });
        setBusy(true);
        showTyping();

        fetch(chatUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken
            },
            body: JSON.stringify({ messages: messages, session: SESSION, page: location.href })
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (r) {
                hideTyping();
                if (r.ok && r.data.reply) {
                    addBubble('assistant', r.data.reply);
                    messages.push({ role: 'assistant', content: r.data.reply });
                    gotReply = true;
                } else {
                    addBubble('assistant', r.data.error ||
                        'Perdona, algo salió mal. Usa el formulario de contacto aquí abajo.');
                }
            })
            .catch(function () {
                hideTyping();
                addBubble('assistant',
                    'Problema de conexión — revisa tu red o usa el formulario de contacto.');
            })
            .then(function () {
                setBusy(false);
                input.focus();
            });
    }

    fab.addEventListener('click', function () {
        widget.classList.contains('is-open') ? closePanel() : openPanel();
    });
    closeBtn.addEventListener('click', closePanel);

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && widget.classList.contains('is-open')) closePanel();
    });

    // Any element with [data-chat-open] (e.g. the section's "Try the live demo") opens the chat.
    document.querySelectorAll('[data-chat-open]').forEach(function (el) {
        el.addEventListener('click', function (e) {
            e.preventDefault();
            openPanel();
        });
    });

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var text = input.value.trim();
        if (!text || busy) return;
        input.value = '';
        send(text);
    });
})();
