/* DOMINIO human check (hCaptcha) for the first-party chats (home chat and the
 * /get-started/ demo). The embeddable widget.js carries its own copy of this
 * logic because it must stay a single self-contained file on customer sites.
 *
 * Flow: every chat POST sends `visitor` (stable per browser tab session) and
 * `human` (the signed pass, if any). When the server answers 403 with
 * {challenge: true}, `challenge()` opens /verificar/ in an overlay iframe; the
 * iframe posts {type: 'dominio-human-pass', pass, visitor} back, which is only
 * accepted from that exact iframe window AND the challenge's own origin.
 * While hCaptcha is not configured the server never asks, so nothing opens.
 */
(function () {
    'use strict';
    if (window.DominioHuman) return;

    var STYLE_ID = 'dmn-hv-style';
    var CSS = ''
        + '.dmn-hv{position:fixed;inset:0;z-index:2147483600;display:flex;align-items:center;'
        + 'justify-content:center;padding:12px;background:rgba(5,12,20,.62)}'
        + '.dmn-hv__box{position:relative;display:flex;flex-direction:column;width:400px;max-width:100%;'
        + 'height:580px;max-height:100%;overflow:hidden;border-radius:14px;background:#fff;'
        + 'box-shadow:0 24px 60px rgba(0,0,0,.45)}'
        + '.dmn-hv__bar{display:flex;justify-content:flex-end;flex:0 0 auto}'
        + '.dmn-hv__x{min-width:44px;min-height:44px;border:0;background:none;color:#1b2733;'
        + 'font-size:24px;line-height:1;cursor:pointer}'
        + '.dmn-hv__x:focus-visible{outline:3px solid #34d6c8;outline-offset:-3px}'
        + '.dmn-hv__frame{flex:1 1 auto;width:100%;min-height:0;border:0;overflow:auto}';

    function randomId() {
        try {
            var a = new Uint8Array(16); crypto.getRandomValues(a);
            return Array.prototype.map.call(a, function (b) {
                return ('0' + b.toString(16)).slice(-2);
            }).join('');
        } catch (e) { return 'v' + Date.now() + Math.floor(Math.random() * 1e9); }
    }

    function readStore(k) { try { return sessionStorage.getItem(k) || ''; } catch (e) { return ''; } }
    function writeStore(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } }

    var memVisitor = '';
    function visitorId() {
        var v = readStore('dmn-visitor') || memVisitor;
        if (!/^[A-Za-z0-9_-]{8,64}$/.test(v)) {
            v = randomId();
            writeStore('dmn-visitor', v);
        }
        memVisitor = v;
        return v;
    }

    function create(opts) {
        var url = null;
        try { url = new URL(opts.challengeUrl, location.href); } catch (e) { url = null; }
        var challengeOrigin = url ? url.origin : '';
        var passKey = 'dmn-human:' + opts.key;
        var memPass = '';
        var visitor = visitorId();
        var overlay = null, frame = null, onPass = null, onCancel = null, lastFocus = null;

        function getPass() { return readStore(passKey) || memPass; }
        function setPass(p) { memPass = p; writeStore(passKey, p); }

        function close(cancelled) {
            if (!overlay) return;
            var cb = cancelled ? onCancel : null;
            overlay.remove();
            overlay = frame = null; onPass = onCancel = null;
            if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) { /* gone */ } }
            if (cb) cb();
        }

        function open(passCb, cancelCb) {
            if (overlay || !url) { if (!url && cancelCb) cancelCb(); return; }
            if (!document.getElementById(STYLE_ID)) {
                var st = document.createElement('style');
                st.id = STYLE_ID; st.textContent = CSS;
                document.head.appendChild(st);
            }
            onPass = passCb; onCancel = cancelCb || null;
            lastFocus = document.activeElement;

            var src = new URL(url.href);
            src.searchParams.set('key', opts.key);
            src.searchParams.set('visitor', visitor);
            src.searchParams.set('origin', location.origin);

            overlay = document.createElement('div');
            overlay.className = 'dmn-hv';
            var box = document.createElement('div');
            box.className = 'dmn-hv__box';
            box.setAttribute('role', 'dialog');
            box.setAttribute('aria-modal', 'true');
            box.setAttribute('aria-label', 'Verificación de seguridad');
            var bar = document.createElement('div');
            bar.className = 'dmn-hv__bar';
            var x = document.createElement('button');
            x.type = 'button'; x.className = 'dmn-hv__x';
            x.setAttribute('aria-label', 'Cerrar verificación');
            x.textContent = '×';
            x.addEventListener('click', function () { close(true); });
            bar.appendChild(x);
            frame = document.createElement('iframe');
            frame.className = 'dmn-hv__frame';
            frame.title = 'Verificación de seguridad';
            frame.src = src.href;
            box.appendChild(bar); box.appendChild(frame);
            overlay.appendChild(box);
            document.body.appendChild(overlay);
            x.focus();
        }

        window.addEventListener('message', function (e) {
            if (!frame || e.source !== frame.contentWindow || e.origin !== challengeOrigin) return;
            var d = e.data;
            if (!d || typeof d !== 'object' || d.type !== 'dominio-human-pass'
                || typeof d.pass !== 'string' || d.visitor !== visitor) return;
            setPass(d.pass);
            var cb = onPass;
            onCancel = null;
            close(false);
            if (cb) cb();
        });

        // Escape closes the challenge first, without also closing the chat.
        window.addEventListener('keydown', function (e) {
            if (overlay && e.key === 'Escape') { e.stopPropagation(); close(true); }
        }, true);

        return {
            visitor: visitor,
            pass: getPass,
            challenge: open,
            isOpen: function () { return !!overlay; }
        };
    }

    window.DominioHuman = { create: create };
})();
