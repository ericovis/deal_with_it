/* Fetches a card's other tabs on the first sign someone will want them.
 *
 * The tabs a card is not showing ship `loading="lazy"`, and a `display: none`
 * image is never near the viewport, so without this each one waits for its
 * own click: the switch is pressed, then a second of nothing, then the
 * picture. Reaching for the switch is a good enough prediction that all of
 * it is wanted, so the first hover, press, focus or change warms every tab
 * at once and the click after it lands on a picture that is already there.
 *
 * Flipping `loading` is what starts the fetch — the spec resumes a deferred
 * load when the hint goes from lazy to eager — so this is the one request
 * the image was always going to make, moved earlier. Never a second one.
 *
 * An enhancement, like the rest: with no script every tab still loads, just
 * at the moment it is chosen. Delegated from the document, because htmx
 * swaps these cards in and out.
 */
(function () {
    'use strict';

    // A hover or a press is a pointer event; arrowing through the radios is
    // neither, and `change` is what a keyboard leaves behind.
    var INTENT = ['pointerover', 'pointerdown', 'focusin', 'change'];

    function warm(control) {
        var result = control.closest('.result');
        // Once per card. The flag also stops a pointer crossing the four
        // labels of the switch from re-walking the DOM on every one.
        if (!result || result.dataset.warmed) { return; }
        result.dataset.warmed = '1';
        result.querySelectorAll('.frame img[loading="lazy"]').forEach(function (img) {
            img.loading = 'eager';
        });
    }

    function onIntent(event) {
        var target = event.target;
        // Both switches: the footer's segmented control and the phone's copy
        // of it on the picture. Either one means the same thing.
        var control = target && target.closest && target.closest('.segmented');
        if (control) { warm(control); }
    }

    INTENT.forEach(function (name) {
        document.addEventListener(name, onIntent);
    });
})();
