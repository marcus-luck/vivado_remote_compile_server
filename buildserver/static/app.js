// The things htmx attributes cannot do on their own.
(function () {
  'use strict';

  // The session expired or the user was deactivated: reload to the sign-in
  // page instead of polling into the void.
  document.body.addEventListener('htmx:responseError', function (e) {
    if (e.detail.xhr.status === 401) { window.location.reload(); }
  });

  // Upload progress bar.
  document.body.addEventListener('htmx:xhr:progress', function (e) {
    var bar = document.getElementById('upload-progress');
    if (bar && e.detail.lengthComputable) {
      bar.value = 100 * e.detail.loaded / e.detail.total;
    }
  });

  // Show server (UTC) timestamps in the browser's local time.
  htmx.onLoad(function (root) {
    root.querySelectorAll('time[datetime]').forEach(function (el) {
      var date = new Date(el.getAttribute('datetime'));
      if (!isNaN(date)) { el.textContent = date.toLocaleString('sv-SE'); }
    });
  });
})();
