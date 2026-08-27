// ===== REDUCED MOTION FLAG =====
const REDUCE_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if (REDUCE_MOTION) document.documentElement.classList.add('reduce-motion');

// ===== HERO WORD-BY-WORD ENTRANCE PREP =====
(function splitHeroWords() {
    const heroContent = document.querySelector('.hero-content');
    if (!heroContent) return;

    let wordIndex = 0;
    const textElements = heroContent.querySelectorAll('.hero-tag, h1, .hero-desc');

    textElements.forEach((el) => {
        const textNodes = [];
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) textNodes.push(node);

        textNodes.forEach((textNode) => {
            const parts = textNode.textContent.split(/(\s+)/);
            const fragment = document.createDocumentFragment();
            parts.forEach((part) => {
                if (!part) return;
                if (/\S/.test(part)) {
                    const span = document.createElement('span');
                    span.className = 'hero-word';
                    span.style.setProperty('--i', wordIndex++);
                    span.textContent = part;
                    fragment.appendChild(span);
                } else {
                    fragment.appendChild(document.createTextNode(part));
                }
            });
            textNode.parentNode.replaceChild(fragment, textNode);
        });
    });

    heroContent.querySelectorAll('.hero-actions a').forEach((btn) => {
        btn.style.setProperty('--i', wordIndex++);
    });
})();

// ===== LOADING SCREEN (domino assembling — on every load) =====
function initDominoLoader() {
    const loader = document.getElementById('loaderOverlay');
    const heroContent = document.querySelector('.hero-content');
    const revealHero = () => heroContent && heroContent.classList.add('hero-content--ready');

    if (!loader) { revealHero(); return; }

    // Reveal the hero immediately, hidden UNDER the opaque overlay, so the LCP
    // <h1> is already painted by the time the overlay lifts (don't serialize the
    // word reveal AFTER the loader — that pushes LCP out). guardian-performance.
    revealHero();

    const hide = () => {
        loader.classList.add('hidden');
        setTimeout(() => loader.remove(), 350);
    };

    if (REDUCE_MOTION) { hide(); return; }

    // Hold long enough for the spin + pips to finish assembling (~870ms), then
    // fade. Fired on DOMContentLoaded (not `load`) so slow images/fonts can't
    // stretch the loader and hurt LCP. No session gate: shows every load.
    setTimeout(hide, 900);
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDominoLoader);
} else {
    initDominoLoader();
}

document.addEventListener('DOMContentLoaded', () => {

    // ===== MOBILE NAV TOGGLE =====
    const navToggle = document.getElementById('navToggle');
    const navLinks = document.getElementById('navLinks');

    if (navToggle) {
        navToggle.addEventListener('click', () => {
            const willOpen = !navToggle.classList.contains('active');
            navToggle.classList.toggle('active');
            navLinks.classList.toggle('open');
            navToggle.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        });
    }

    document.querySelectorAll('.nav-links a').forEach(link => {
        link.addEventListener('click', () => {
            if (navToggle) {
                navToggle.classList.remove('active');
                navToggle.setAttribute('aria-expanded', 'false');
            }
            navLinks.classList.remove('open');
        });
    });

    // ===== NAVBAR SCROLL EFFECT =====
    const header = document.querySelector('.header');
    window.addEventListener('scroll', () => {
        header.classList.toggle('scrolled', window.scrollY > 50);
    });

    // ===== ACTIVE NAV LINK ON SCROLL =====
    const sections = document.querySelectorAll('section[id]');
    const navAnchorsAll = document.querySelectorAll('.nav-links a');
    window.addEventListener('scroll', () => {
        const scrollY = window.scrollY + 200;
        sections.forEach(section => {
            const top = section.offsetTop;
            const id = section.getAttribute('id');
            if (scrollY >= top && scrollY < top + section.offsetHeight) {
                navAnchorsAll.forEach(a => a.classList.remove('active'));
                const link = document.querySelector(`.nav-links a[href="#${id}"]`);
                if (link) link.classList.add('active');
            }
        });
    });

    // ===== SMOOTH SCROLL =====
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', (e) => {
            e.preventDefault();
            const target = document.querySelector(anchor.getAttribute('href'));
            if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    });

    // ===== FLOATING DOMINOES WITH SNAP-CONNECT =====
    const hero = document.querySelector('.hero');
    const container = document.getElementById('dominosContainer');

    if (container && hero) {
        const isMobile = window.innerWidth <= 768;
        const DOMINO_W = isMobile ? 45 : 70;
        const DOMINO_H = isMobile ? 85 : 130;
        const SNAP_DIST = isMobile ? 40 : 60;

        const dotLayouts = {
            0: [],
            1: ['dot-mc'],
            2: ['dot-tr', 'dot-bl'],
            3: ['dot-tr', 'dot-mc', 'dot-bl'],
            4: ['dot-tl', 'dot-tr', 'dot-bl', 'dot-br'],
            5: ['dot-tl', 'dot-tr', 'dot-mc', 'dot-bl', 'dot-br'],
            6: ['dot-tl', 'dot-tr', 'dot-ml', 'dot-mr', 'dot-bl', 'dot-br'],
        };

        // Pieces to spawn (subset of full 28)
        const pieceDefs = [
            [1, 4], [3, 3], [0, 5], [2, 6], [1, 1], [4, 5], [2, 3],
            [0, 3], [5, 6], [3, 4],
        ];

        const dominoes = []; // { el, top, bottom, x, y, connectedTo: null }

        function createHalf(num) {
            const half = document.createElement('div');
            half.className = 'domino-half';
            dotLayouts[num].forEach(pos => {
                const dot = document.createElement('div');
                dot.className = 'domino-dot ' + pos;
                half.appendChild(dot);
            });
            return half;
        }

        function spawnDominoes() {
            const heroW = hero.offsetWidth;
            const heroH = hero.offsetHeight;

            pieceDefs.forEach(([top, bottom], i) => {
                const el = document.createElement('div');
                el.className = 'domino';

                el.appendChild(createHalf(top));
                const divider = document.createElement('div');
                divider.className = 'domino-divider';
                el.appendChild(divider);
                el.appendChild(createHalf(bottom));

                // Spread around hero, avoiding center text area
                let x, y;
                do {
                    x = 30 + Math.random() * (heroW - DOMINO_W - 60);
                    y = 30 + Math.random() * (heroH - DOMINO_H - 60);
                } while (x > heroW * 0.15 && x < heroW * 0.65 &&
                         y > heroH * 0.2 && y < heroH * 0.75);

                const rotation = -20 + Math.random() * 40;
                el.style.left = x + 'px';
                el.style.top = y + 'px';
                el.style.transform = `rotate(${rotation}deg)`;

                // Float animation
                const dur = 5 + Math.random() * 4;
                const delay = Math.random() * -8;
                el.style.animation = `dominoFloat ${dur}s ${delay}s ease-in-out infinite`;

                container.appendChild(el);

                dominoes.push({
                    el, top, bottom, x, y, rotation,
                    isDouble: top === bottom,
                    isHorizontal: false,
                    connections: { top: null, bottom: null, left: null, right: null },
                });
            });
        }

        // Float keyframes
        const floatStyle = document.createElement('style');
        floatStyle.textContent = `
            @keyframes dominoFloat {
                0%, 100% { transform: translate(0, 0) rotate(var(--rot, 0deg)); }
                25%  { transform: translate(12px, -16px) rotate(calc(var(--rot, 0deg) + 2deg)); }
                50%  { transform: translate(-8px, -22px) rotate(calc(var(--rot, 0deg) - 1.5deg)); }
                75%  { transform: translate(10px, -10px) rotate(calc(var(--rot, 0deg) + 1deg)); }
            }
        `;
        document.head.appendChild(floatStyle);

        spawnDominoes();
        dominoes.forEach(d => d.el.style.setProperty('--rot', d.rotation + 'deg'));

        // ===== DRAG + SNAP LOGIC =====
        let active = null;
        let offset = { x: 0, y: 0 };

        function hasAnyConnection(d) {
            return d.connections.top !== null || d.connections.bottom !== null ||
                   d.connections.left !== null || d.connections.right !== null;
        }

        function disconnectDomino(d) {
            for (const side of ['top', 'bottom', 'left', 'right']) {
                const conn = d.connections[side];
                if (conn) {
                    const other = conn.domino;
                    other.connections[conn.side] = null;
                    if (!hasAnyConnection(other)) {
                        other.el.classList.remove('connected');
                    }
                    d.connections[side] = null;
                }
            }
            d.el.classList.remove('connected');
            if (d.isHorizontal) {
                d.isHorizontal = false;
                d.el.classList.remove('domino-horizontal');
            }
        }

        function getEffW(d) { return d.isHorizontal ? DOMINO_H : DOMINO_W; }
        function getEffH(d) { return d.isHorizontal ? DOMINO_W : DOMINO_H; }

        function startDrag(el, cx, cy) {
            const d = dominoes.find(item => item.el === el);
            if (!d) return;
            active = d;
            disconnectDomino(d);
            el.classList.add('dragging-domino');
            el.style.animation = 'none';
            const rect = el.getBoundingClientRect();
            offset.x = cx - rect.left;
            offset.y = cy - rect.top;
        }

        function moveDrag(cx, cy) {
            if (!active) return;
            const heroRect = hero.getBoundingClientRect();
            const x = cx - heroRect.left - offset.x;
            const y = cy - heroRect.top - offset.y;
            active.el.style.left = x + 'px';
            active.el.style.top = y + 'px';
            active.el.style.transform = 'rotate(0deg)';
            active.x = x;
            active.y = y;
        }

        function endDrag() {
            if (!active) return;
            active.el.classList.remove('dragging-domino');

            // Check for snap
            const snapped = trySnap(active);

            if (!snapped) {
                // Resume floating
                const dur = 5 + Math.random() * 4;
                active.el.style.transform = `rotate(${active.rotation}deg)`;
                active.el.style.animation = `dominoFloat ${dur}s ease-in-out infinite`;
            }

            active = null;
        }

        function isAlreadyConnected(d, side) {
            return d.connections[side] !== null;
        }

        // Get the value exposed on a given side of a domino
        // top/left always expose d.top, bottom/right always expose d.bottom
        function getValueOnSide(d, side) {
            if (side === 'top' || side === 'left') return d.top;
            if (side === 'bottom' || side === 'right') return d.bottom;
            return null;
        }

        function trySnap(dragged) {
            let bestMatch = null;
            let bestDist = SNAP_DIST;

            // Dragged is always vertical when being moved (disconnectDomino resets it)
            const dW = DOMINO_W;
            const dH = DOMINO_H;
            const dCX = dragged.x + dW / 2;
            const dCY = dragged.y + dH / 2;
            const MAX_ALIGN = 25;
            const GAP = 4;

            for (const other of dominoes) {
                if (other === dragged) continue;

                const oW = getEffW(other);
                const oH = getEffH(other);
                const oCX = other.x + oW / 2;
                const oCY = other.y + oH / 2;

                const xGap = Math.abs(dCX - oCX);
                const yGap = Math.abs(dCY - oCY);

                // === VERTICAL CONNECTIONS (dragged stays vertical) ===
                const dBotY = dragged.y + dH * 0.75;
                const dTopY = dragged.y + dH * 0.25;
                const oTopY = other.y + oH * 0.25;
                const oBotY = other.y + oH * 0.75;

                // Case 1: dragged above other — dragged.bottom matches other.top
                const dist1 = Math.hypot(dCX - oCX, dBotY - oTopY);
                if (dist1 < bestDist && xGap < MAX_ALIGN &&
                    dragged.bottom === other.top &&
                    !isAlreadyConnected(dragged, 'bottom') &&
                    !isAlreadyConnected(other, 'top')) {
                    bestDist = dist1;
                    bestMatch = { other, dragSide: 'bottom', otherSide: 'top', goHorizontal: false };
                }

                // Case 2: dragged below other — dragged.top matches other.bottom
                const dist2 = Math.hypot(dCX - oCX, dTopY - oBotY);
                if (dist2 < bestDist && xGap < MAX_ALIGN &&
                    dragged.top === other.bottom &&
                    !isAlreadyConnected(dragged, 'top') &&
                    !isAlreadyConnected(other, 'bottom')) {
                    bestDist = dist2;
                    bestMatch = { other, dragSide: 'top', otherSide: 'bottom', goHorizontal: false };
                }

                // Case 3: dragged above other — dragged.top matches other.top (same number facing same way)
                if (dist1 < bestDist && xGap < MAX_ALIGN &&
                    dragged.top === other.top &&
                    !isAlreadyConnected(dragged, 'bottom') &&
                    !isAlreadyConnected(other, 'top')) {
                    bestDist = dist1;
                    bestMatch = { other, dragSide: 'bottom', otherSide: 'top', goHorizontal: false, flipDragged: true };
                }

                // Case 4: dragged below other — dragged.bottom matches other.bottom (same number facing same way)
                if (dist2 < bestDist && xGap < MAX_ALIGN &&
                    dragged.bottom === other.bottom &&
                    !isAlreadyConnected(dragged, 'top') &&
                    !isAlreadyConnected(other, 'bottom')) {
                    bestDist = dist2;
                    bestMatch = { other, dragSide: 'top', otherSide: 'bottom', goHorizontal: false, flipDragged: true };
                }

                // === HORIZONTAL CONNECTIONS (dragged rotates to horizontal) ===
                const dHorizW = DOMINO_H;

                // Case 5: dragged to left of other — dragged.bottom matches other.top (left val)
                const projRightX = dCX + dHorizW / 2;
                const dist5 = Math.hypot(projRightX - other.x, dCY - oCY);
                if (dist5 < bestDist && yGap < MAX_ALIGN &&
                    (dragged.bottom === other.top || dragged.top === other.top) &&
                    !isAlreadyConnected(dragged, 'right') &&
                    !isAlreadyConnected(other, 'left')) {
                    const needFlip = dragged.bottom !== other.top;
                    bestDist = dist5;
                    bestMatch = { other, dragSide: 'right', otherSide: 'left', goHorizontal: true, flipDragged: needFlip };
                }

                // Case 6: dragged to right of other — dragged.top matches other.bottom (right val)
                const projLeftX = dCX - dHorizW / 2;
                const dist6 = Math.hypot(projLeftX - (other.x + oW), dCY - oCY);
                if (dist6 < bestDist && yGap < MAX_ALIGN &&
                    (dragged.top === other.bottom || dragged.bottom === other.bottom) &&
                    !isAlreadyConnected(dragged, 'left') &&
                    !isAlreadyConnected(other, 'right')) {
                    const needFlip = dragged.top !== other.bottom;
                    bestDist = dist6;
                    bestMatch = { other, dragSide: 'left', otherSide: 'right', goHorizontal: true, flipDragged: needFlip };
                }
            }

            if (!bestMatch) return false;

            const { other, dragSide, otherSide, goHorizontal, flipDragged } = bestMatch;

            // Flip the domino values if needed (swap top/bottom visually)
            if (flipDragged) {
                const tmp = dragged.top;
                dragged.top = dragged.bottom;
                dragged.bottom = tmp;
                // Rebuild the domino halves in reverse order
                const halves = dragged.el.querySelectorAll('.domino-half');
                const divider = dragged.el.querySelector('.domino-divider');
                dragged.el.innerHTML = '';
                dragged.el.appendChild(halves[1]);
                dragged.el.appendChild(divider);
                dragged.el.appendChild(halves[0]);
            }

            // Rotate dragged to horizontal if needed
            if (goHorizontal || dragged.isDouble) {
                dragged.isHorizontal = true;
                dragged.el.classList.add('domino-horizontal');
            }

            const dEffW = getEffW(dragged);
            const dEffH = getEffH(dragged);
            const oEffW = getEffW(other);
            const oEffH = getEffH(other);

            let snapX, snapY;

            if (dragSide === 'bottom') {
                // Dragged sits above other
                snapX = other.x + oEffW / 2 - dEffW / 2;
                snapY = other.y - dEffH - GAP;
            } else if (dragSide === 'top') {
                // Dragged sits below other
                snapX = other.x + oEffW / 2 - dEffW / 2;
                snapY = other.y + oEffH + GAP;
            } else if (dragSide === 'right') {
                // Dragged sits to the left of other
                snapX = other.x - dEffW - GAP;
                snapY = other.y + oEffH / 2 - dEffH / 2;
            } else if (dragSide === 'left') {
                // Dragged sits to the right of other
                snapX = other.x + oEffW + GAP;
                snapY = other.y + oEffH / 2 - dEffH / 2;
            }

            dragged.x = snapX;
            dragged.y = snapY;
            dragged.el.style.left = snapX + 'px';
            dragged.el.style.top = snapY + 'px';
            dragged.el.style.transform = 'rotate(0deg)';
            dragged.el.style.animation = 'none';
            dragged.rotation = 0;

            // Straighten the other piece
            other.el.style.transform = 'rotate(0deg)';
            other.el.style.animation = 'none';
            other.rotation = 0;

            // Record bidirectional connections
            dragged.connections[dragSide] = { domino: other, side: otherSide };
            other.connections[otherSide] = { domino: dragged, side: dragSide };

            dragged.el.classList.add('connected', 'snap-flash');
            other.el.classList.add('connected', 'snap-flash');

            setTimeout(() => {
                dragged.el.classList.remove('snap-flash');
                other.el.classList.remove('snap-flash');
            }, 500);

            return true;
        }

        // Mouse events
        container.addEventListener('mousedown', (e) => {
            const el = e.target.closest('.domino');
            if (!el) return;
            e.preventDefault();
            startDrag(el, e.clientX, e.clientY);
        });

        window.addEventListener('mousemove', (e) => {
            if (active) {
                e.preventDefault();
                moveDrag(e.clientX, e.clientY);
            }
        });

        window.addEventListener('mouseup', endDrag);

        // Touch events
        container.addEventListener('touchstart', (e) => {
            const el = e.target.closest('.domino');
            if (!el) return;
            startDrag(el, e.touches[0].clientX, e.touches[0].clientY);
        }, { passive: true });

        window.addEventListener('touchmove', (e) => {
            if (active) {
                e.preventDefault();
                moveDrag(e.touches[0].clientX, e.touches[0].clientY);
            }
        }, { passive: false });

        window.addEventListener('touchend', endDrag);
    }

    // ===== PER-ITEM REVEAL SYSTEM =====
    // Each reveal-item mounts when THAT element clearly enters the viewport — not
    // when its (tall) parent section first peeks in — so items low in a section
    // don't animate while still below the fold. The .reveal-section ancestor only
    // carries the hidden initial state (content stays hidden pre-JS / no-JS).
    (function initItemReveals() {
        const items = document.querySelectorAll('.reveal-section .reveal-item');
        if (!items.length) return;

        if (REDUCE_MOTION) {
            items.forEach((el) => el.classList.add('is-visible'));
            return;
        }

        const itemObserver = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('is-visible');
                        itemObserver.unobserve(entry.target);
                    }
                });
            },
            // Fire as the item crosses into the viewport (minus a small bottom
            // band so it reveals just as it reaches the screen). threshold 0 so
            // items at the very bottom of the page still reveal at max scroll.
            { threshold: 0, rootMargin: '0px 0px -10% 0px' }
        );

        items.forEach((el) => itemObserver.observe(el));
    })();

    // ===== PER-CARD PIP REVEAL =====
    // Domino tiles assemble per card, fired when THAT card is clearly in view —
    // not when the tall parent section first peeks in — so cards below the fold
    // don't build off-screen.
    (function initPipReveals() {
        const cards = document.querySelectorAll('.problem-card, .product-card');
        if (!cards.length) return;

        if (REDUCE_MOTION) {
            cards.forEach((c) => c.classList.add('pips-in'));
            return;
        }

        const pipObserver = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('pips-in');
                        pipObserver.unobserve(entry.target);
                    }
                });
            },
            // ~30% of the card in view so the build happens where the eye is.
            { threshold: 0.3, rootMargin: '0px 0px -10% 0px' }
        );

        cards.forEach((card) => pipObserver.observe(card));
    })();

    // ===== PROCESS TIMELINE =====
    // One trigger for the whole process timeline: when it reaches the screen the
    // connecting line draws AND the pip glow chase (1→2→3→4) starts in sync.
    (function initProcessTimeline() {
        const tl = document.querySelector('.process-timeline');
        if (!tl) return;

        if (REDUCE_MOTION) {
            tl.classList.add('in-view');
            return;
        }

        const tlObserver = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('in-view');
                        tlObserver.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.25, rootMargin: '0px 0px -10% 0px' }
        );

        tlObserver.observe(tl);
    })();

    // ===== MOBILE TAP-TO-FLIP =====
    if (window.innerWidth <= 768) {
        const FLIP_DURATION = 600; // matches CSS transition duration

        // Pre-measure both faces of each flippable card
        function measureFaces(card, frontSel, backSel) {
            const front = card.querySelector(frontSel);
            const back = card.querySelector(backSel);
            if (!front || !back) return;

            // Front height is natural (already in-flow)
            const frontH = front.offsetHeight;

            // Temporarily show back face to measure it
            back.style.position = 'relative';
            back.style.transform = 'none';
            back.style.visibility = 'hidden';
            front.style.display = 'none';
            const backH = back.offsetHeight;
            // Restore
            front.style.display = '';
            back.style.position = '';
            back.style.transform = '';
            back.style.visibility = '';

            card.dataset.frontHeight = frontH;
            card.dataset.backHeight = backH;
        }

        const bentoCards = document.querySelectorAll('.bento-item--flippable');
        const visualCards = document.querySelectorAll('.visual-card');

        // Each measureFaces() call forces a synchronous reflow; doing them all
        // on DOMContentLoaded cost a ~390ms long task on mobile, right as the
        // loader lifts. Measure when the thread is idle instead, and lazily on
        // first flip for anyone who taps before the idle callback runs.
        function ensureMeasured(card) {
            if (card.dataset.frontHeight !== undefined) return;
            const isBento = card.classList.contains('bento-item--flippable');
            measureFaces(card,
                isBento ? '.bento-flip-front' : '.visual-flip-front',
                isBento ? '.bento-flip-back' : '.visual-flip-back');
        }

        const idle = window.requestIdleCallback || function (fn) { return setTimeout(fn, 200); };
        idle(function () {
            bentoCards.forEach(ensureMeasured);
            visualCards.forEach(ensureMeasured);
        });

        // Smooth JS-driven height animation (bypasses CSS transition issues)
        function animateHeight(card, from, to, duration, delay) {
            if (card._heightAnim) cancelAnimationFrame(card._heightAnim);
            const start = performance.now() + (delay || 0);
            function tick(now) {
                const elapsed = now - start;
                if (elapsed < 0) {
                    card._heightAnim = requestAnimationFrame(tick);
                    return;
                }
                const t = Math.min(elapsed / duration, 1);
                // Ease-out cubic for smooth deceleration
                const ease = 1 - Math.pow(1 - t, 3);
                const current = from + (to - from) * ease;
                card.style.minHeight = current + 'px';
                if (t < 1) {
                    card._heightAnim = requestAnimationFrame(tick);
                } else {
                    card._heightAnim = null;
                }
            }
            card._heightAnim = requestAnimationFrame(tick);
        }

        function openCard(card) {
            ensureMeasured(card);
            const frontH = parseInt(card.dataset.frontHeight, 10) || 0;
            const backH = parseInt(card.dataset.backHeight, 10) || 0;
            card.style.minHeight = frontH + 'px';
            card.classList.add('flipped');
            animateHeight(card, frontH, backH, 700);
        }

        function closeCard(card) {
            ensureMeasured(card);
            const frontH = parseInt(card.dataset.frontHeight, 10) || 0;
            const backH = parseInt(card.dataset.backHeight, 10) || 0;
            const currentH = card.getBoundingClientRect().height;
            card.classList.remove('flipped');
            // Start shrinking after a short delay so flip begins first
            animateHeight(card, currentH, frontH, 850, 150);
        }

        function setupFlippable(cards) {
            cards.forEach(card => {
                card.setAttribute('role', 'button');
                card.setAttribute('tabindex', '0');
                card.setAttribute('aria-expanded', 'false');
                const toggle = () => {
                    const isOpen = card.classList.contains('flipped');
                    cards.forEach(c => {
                        if (c !== card) {
                            closeCard(c);
                            c.setAttribute('aria-expanded', 'false');
                        }
                    });
                    if (isOpen) {
                        closeCard(card);
                        card.setAttribute('aria-expanded', 'false');
                    } else {
                        openCard(card);
                        card.setAttribute('aria-expanded', 'true');
                    }
                };
                card.addEventListener('click', toggle);
                card.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        toggle();
                    }
                });
            });
        }

        setupFlippable(bentoCards);
        setupFlippable(visualCards);
    }
});
