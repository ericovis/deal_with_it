/* Fetches a card's other tabs the moment the card lands, one after another.
 *
 * The tabs a card is not showing ship `loading="lazy"`, and a `display: none`
 * image is never near the viewport, so without this each one waits for its
 * own click: the switch is pressed, then a second of nothing, then the
 * picture. Nobody opens a result to look at one tab, so all of them are
 * fetched up front and every press after that is instant.
 *
 * In order, and one at a time: GIF, then After, then Before. The GIF is the
 * tab the card opens on, so it must not be made to share a connection with
 * two pictures nobody is looking at yet; each of the others starts when the
 * one before it has finished. Order is the whole point — released together
 * they would be three downloads racing, and the visible one would lose.
 *
 * Flipping `loading` is what starts the fetch — the spec resumes a deferred
 * load when the hint goes from lazy to eager — so these are the requests the
 * images were always going to make, moved earlier. Never a second one, and a
 * tab clicked before its turn simply loads on being shown, as it always did.
 *
 * It also drops `data-ready` on the card once the tab it opens on has
 * painted, which is what retires the skeleton in `style.css`. That is a
 * tidy-up, not the mechanism: the picture covers the skeleton by painting
 * over it, so a browser running no script here still shows the result.
 *
 * An enhancement, like the rest: with no script every tab still loads, just
 * at the moment it is chosen. Delegated from the document, because htmx
 * swaps these cards in and out.
 */
(function () {
    'use strict';

    // Most wanted first. The card opens on the animation; After is the
    // sharp picture the full-screen view wants; Before is a curiosity.
    var ORDER = ['animated', 'after', 'before'];

    function settled(image) {
        if (image.complete) { return Promise.resolve(); }
        return new Promise(function (resolve) {
            // `error` too: one picture that will not load must not hold back
            // the two behind it for as long as the browser cares to retry.
            image.addEventListener('load', resolve, {once: true});
            image.addEventListener('error', resolve, {once: true});
        });
    }

    function queue(result) {
        var images = ORDER
            .map(function (name) { return result.querySelector('.frame img.' + name); })
            .filter(Boolean);
        // A job that wrote one picture has no switch, so its image carries no
        // tab class. There is nothing to order, but there is still a skeleton
        // waiting to be told the picture arrived.
        return images.length ? images : [].slice.call(result.querySelectorAll('.frame img'));
    }

    async function warm(result) {
        // Once per card. htmx re-swaps a finished card on nothing, but the
        // share page and a re-render would both arrive here twice.
        if (result.dataset.warmed) { return; }
        result.dataset.warmed = '1';
        var images = queue(result);
        for (var i = 0; i < images.length; i++) {
            images[i].loading = 'eager';
            await settled(images[i]);
            // The first is the tab the card opens on: once it has painted
            // there is nothing left for the skeleton to stand in for.
            if (i === 0) { result.dataset.ready = '1'; }
        }
    }

    function sweep() {
        document.querySelectorAll('.result:not([data-warmed])').forEach(warm);
    }

    document.addEventListener('htmx:afterSwap', sweep);
    sweep();
})();
