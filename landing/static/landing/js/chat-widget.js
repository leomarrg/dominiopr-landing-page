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

    var GREETING = "Hi, welcome to DOMINIO. What are you looking to build? " +
        "Pick one to get started, or just type your question. " +
        "(Escríbeme en español si prefieres.)";

    // Service quick-picks shown first, so visitors choose instead of typing.
    var SERVICE_OPTIONS = [
        { label: 'Custom platform', message: "I'm interested in a custom operational platform." },
        { label: 'Dashboards & data', message: "I'm interested in dashboards and reporting systems." },
        { label: 'AI agent / automation', message: "I'm interested in an AI agent or automation." },
        { label: 'Landing page & forms', message: "I need a landing page, forms, or a campaign system." },
        { label: 'Strategy & consulting', message: "I'd like product strategy or technical consulting." }
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

    function closePanel() {
        widget.classList.remove('is-open');
        panel.setAttribute('aria-hidden', 'true');
        fab.setAttribute('aria-expanded', 'false');
    }

    function addBubble(role, text) {
        var bubble = document.createElement('div');
        bubble.className = 'chat-bubble chat-bubble--' + role;
        bubble.textContent = text;
        log.appendChild(bubble);
        log.scrollTop = log.scrollHeight;
        return bubble;
    }

    function showTyping() {
        var t = document.createElement('div');
        t.className = 'chat-bubble chat-bubble--assistant chat-typing';
        t.id = 'chatTyping';
        t.innerHTML = '<span></span><span></span><span></span>';
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
            body: JSON.stringify({ messages: messages })
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
                } else {
                    addBubble('assistant', r.data.error ||
                        'Sorry, something went wrong. Please try the contact form below.');
                }
            })
            .catch(function () {
                hideTyping();
                addBubble('assistant',
                    'Connection issue — please check your network or use the contact form.');
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
