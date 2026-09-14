// Adds a show/hide eye to every password box on the page.
//
// Attaches by scanning for input[type=password] rather than asking each
// template to opt in, so the six existing fields and any added later are
// covered without touching them. Versioned filename because static files
// are served with a one-year cache — bump to -2 if this changes.
(function () {
  const EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px"><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px"><path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"/></svg>';

  function attach(input) {
    if (input.dataset.pwToggle) return;
    input.dataset.pwToggle = '1';

    // Wrap so the button can sit inside the field's right edge without
    // every template needing a position:relative parent of its own.
    const wrap = document.createElement('div');
    wrap.style.position = 'relative';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    input.style.paddingRight = '2.75rem';

    const btn = document.createElement('button');
    btn.type = 'button';            // never submits the form it sits in
    btn.innerHTML = EYE;
    btn.setAttribute('aria-label', 'Show password');
    btn.style.cssText = 'position:absolute;top:50%;right:.75rem;transform:translateY(-50%);' +
                        'background:none;border:0;padding:.25rem;cursor:pointer;color:#9ca3af;' +
                        'display:flex;align-items:center;line-height:0';

    btn.addEventListener('click', function () {
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      btn.innerHTML = showing ? EYE : EYE_OFF;
      btn.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
      input.focus();
    });

    wrap.appendChild(btn);
  }

  function scan() {
    document.querySelectorAll('input[type=password]').forEach(attach);
  }

  document.addEventListener('DOMContentLoaded', scan);
  // Password fields inside Alpine modals (staff reset-password) only exist
  // once the modal opens, so re-scan when the DOM changes.
  new MutationObserver(scan).observe(document.documentElement,
                                     { childList: true, subtree: true });
})();
