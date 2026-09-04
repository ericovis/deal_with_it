/* Turns a server-rendered expiry time into a live countdown.
 *
 * Progressive enhancement, deliberately: the server writes the truth into the
 * element as an absolute time ("Available until 17:32"), which is correct with
 * or without this file. All this does is keep it counting.
 *
 * The DOM is walked on every tick rather than cached, because htmx swaps cards
 * in and out constantly and a held reference would go stale the moment a card
 * refreshed itself. One interval for the page, not one per card.
 */
(function () {
    'use strict';

    function remaining(seconds) {
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        var s = Math.floor(seconds % 60);
        var pad = function (n) { return n < 10 ? '0' + n : String(n); };
        return h > 0 ? h + ':' + pad(m) + ':' + pad(s) : m + ':' + pad(s);
    }

    function tick() {
        var now = Date.now();
        document.querySelectorAll('[data-expires]').forEach(function (el) {
            var at = Date.parse(el.getAttribute('datetime'));
            if (isNaN(at)) { return; }
            var left = (at - now) / 1000;
            if (left <= 0) {
                el.textContent = 'This one has expired';
                el.classList.add('gone');
                return;
            }
            el.textContent = 'Available for another ' + remaining(left);
        });
    }

    // Once immediately so a freshly swapped card does not sit on the
    // server-rendered sentence for up to a second before catching up.
    document.addEventListener('htmx:afterSwap', tick);
    tick();
    setInterval(tick, 1000);
})();
