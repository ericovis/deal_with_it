/* Turns a server-rendered expiry time into a live countdown.
 *
 * Progressive enhancement, deliberately: the server writes the truth into the
 * element as an absolute time ("deleted at 17:32"), which is correct with
 * or without this file. All this does is keep it counting.
 *
 * The DOM is walked on every tick rather than cached, because htmx swaps cards
 * in and out constantly and a held reference would go stale the moment a card
 * refreshed itself. One interval for the page, not one per card.
 */
(function () {
    'use strict';

    /* Rounded down, so it never promises time the picture does not have. */
    function remaining(seconds) {
        if (seconds < 60) { return Math.floor(seconds) + ' s'; }
        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) { return minutes + ' min'; }
        return Math.floor(minutes / 60) + ' h ' + (minutes % 60) + ' min';
    }

    function tick() {
        var now = Date.now();
        document.querySelectorAll('[data-expires]').forEach(function (el) {
            var at = Date.parse(el.getAttribute('datetime'));
            if (isNaN(at)) { return; }
            var left = (at - now) / 1000;
            if (left <= 0) {
                el.textContent = 'This image has been deleted';
                el.classList.add('gone');
                return;
            }
            el.textContent = 'This image will be deleted in ' + remaining(left);
        });
    }

    // Once immediately so a freshly swapped card does not sit on the
    // server-rendered sentence for up to a second before catching up.
    document.addEventListener('htmx:afterSwap', tick);
    tick();
    setInterval(tick, 1000);
})();
