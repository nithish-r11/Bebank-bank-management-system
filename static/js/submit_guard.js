/**
 * Bebank Client-Side Single-Submission Safeguard
 *
 * Prevents accidental duplicate submissions and rapid double-clicks on state-changing forms.
 *
 * Security Notice:
 * This script is strictly a client-side UX safeguard. It does not replace server-side
 * transaction atomicity, row-level locking, CSRF validation, or server-side idempotency keys.
 */
(function () {
    'use strict';

    if (typeof window !== 'undefined' && window.__bebankSubmitGuardInitialized) {
        return;
    }
    if (typeof window !== 'undefined') {
        window.__bebankSubmitGuardInitialized = true;
    }

    function initSubmitGuard() {
        var activeSubmitter = null;

        // Capture the clicked submitter button to support multi-button forms
        document.addEventListener('click', function (event) {
            var target = event.target;
            var button = target.closest ? target.closest('button:not([type="button"]):not([type="reset"]), input[type="submit"]') : null;
            if (button && button.form) {
                activeSubmitter = button;
            }
        }, true);

        // Form submit handler
        document.addEventListener('submit', function (event) {
            var form = event.target;
            if (!form || form.nodeName !== 'FORM') return;

            // Only apply to POST (state-changing) forms
            var method = (form.getAttribute('method') || 'GET').toUpperCase();
            if (method !== 'POST') return;

            // Allow form to opt out explicitly if needed
            if (form.dataset && form.dataset.noSubmitGuard === 'true') return;

            // If another listener or custom validator cancelled the event, do not activate guard
            if (event.defaultPrevented) return;

            // Allow browser constraint validation to run before activating guard
            if (typeof form.checkValidity === 'function' && !form.checkValidity()) {
                return;
            }

            // If form is already submitting, cancel duplicate submit events immediately
            if (form.dataset && form.dataset.submitting === 'true') {
                event.preventDefault();
                if (typeof event.stopImmediatePropagation === 'function') {
                    event.stopImmediatePropagation();
                }
                return false;
            }

            // Mark form as currently submitting
            if (!form.dataset) {
                form.dataset = {};
            }
            form.dataset.submitting = 'true';

            // Find submit buttons belonging to this form
            var submitButtons = form.querySelectorAll(
                'button:not([type="button"]):not([type="reset"]), input[type="submit"]'
            );
            var primaryButton = (event.submitter && form.contains && form.contains(event.submitter))
                ? event.submitter
                : (activeSubmitter && form.contains && form.contains(activeSubmitter)
                    ? activeSubmitter
                    : (submitButtons.length > 0 ? submitButtons[0] : null));

            // Defer button disabling to next microtick so browser dispatches standard form POST
            // with the native submitter name/value preserved
            setTimeout(function () {
                for (var i = 0; i < submitButtons.length; i++) {
                    var btn = submitButtons[i];
                    btn.disabled = true;
                    btn.setAttribute('aria-disabled', 'true');
                }

                if (primaryButton) {
                    if (!primaryButton.dataset) primaryButton.dataset = {};
                    var isInput = primaryButton.nodeName === 'INPUT';
                    primaryButton.dataset.originalText = isInput
                        ? (primaryButton.value || '')
                        : (primaryButton.textContent || primaryButton.innerText || '');

                    if (isInput) {
                        primaryButton.value = 'Processing...';
                    } else {
                        primaryButton.textContent = 'Processing...';
                    }
                }
            }, 0);
        }, false);

        // Handle bfcache (Back-Forward Cache): re-enable buttons when navigating back
        window.addEventListener('pageshow', function (event) {
            if (event.persisted) {
                var activeForms = document.querySelectorAll('form[data-submitting="true"]');
                for (var i = 0; i < activeForms.length; i++) {
                    var form = activeForms[i];
                    if (form.dataset) form.dataset.submitting = 'false';

                    var submitButtons = form.querySelectorAll(
                        'button:not([type="button"]):not([type="reset"]), input[type="submit"]'
                    );
                    for (var j = 0; j < submitButtons.length; j++) {
                        var btn = submitButtons[j];
                        btn.disabled = false;
                        btn.removeAttribute('aria-disabled');
                        if (btn.dataset && btn.dataset.originalText !== undefined) {
                            if (btn.nodeName === 'INPUT') {
                                btn.value = btn.dataset.originalText;
                            } else {
                                btn.textContent = btn.dataset.originalText;
                            }
                        }
                    }
                }
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSubmitGuard);
    } else {
        initSubmitGuard();
    }
})();
