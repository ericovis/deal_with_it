/* Offers the system share sheet, where there is one.
 *
 * On a phone this is what puts a result into Photos, WhatsApp or Messages
 * without a round trip through the downloads folder. There is no way to know
 * whether a browser can share a *file* without asking it, so the button ships
 * hidden and this reveals it — a desktop Chrome that can only share links
 * never shows a button that would half-work.
 *
 * Delegated from the document, because htmx swaps these cards in and out.
 */
(function () {
    'use strict';

    var CAN_SHARE_FILES = !!(navigator.canShare && navigator.share);

    function reveal() {
        if (!CAN_SHARE_FILES) { return; }
        document.querySelectorAll('button.share-system[hidden]').forEach(function (button) {
            button.hidden = false;
        });
    }

    async function share(button) {
        var url = button.getAttribute('data-share');
        var name = button.getAttribute('data-name') || 'deal-with-it';
        button.disabled = true;
        try {
            var response = await fetch(url);
            if (!response.ok) { return; }
            var blob = await response.blob();
            var extension = (blob.type.split('/')[1] || 'jpg').replace('jpeg', 'jpg');
            var file = new File([blob], name + '.' + extension, {type: blob.type});
            if (!navigator.canShare({files: [file]})) { return; }
            await navigator.share({files: [file], title: 'Deal With It!'});
        } catch (error) {
            // Dismissing the sheet rejects with AbortError, which is a
            // person changing their mind, not a fault worth reporting.
            if (error && error.name !== 'AbortError') {
                console.warn('sharing failed', error);
            }
        } finally {
            button.disabled = false;
        }
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest && event.target.closest('button.share-system');
        if (button) { share(button); }
    });
    document.addEventListener('htmx:afterSwap', reveal);
    reveal();
})();
