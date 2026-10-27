// Click (or Enter on) a photo with class "zoom" to see it larger; click anywhere, or Escape, to close.
(function () {
  var dlg = null;
  function open(img) {
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.className = 'photo-zoom';
      dlg.innerHTML = '<figure><img alt=""><figcaption></figcaption></figure>';
      dlg.addEventListener('click', function () { dlg.close(); });
      document.body.appendChild(dlg);
    }
    dlg.querySelector('img').src = img.src;
    dlg.querySelector('img').alt = img.alt;
    dlg.querySelector('figcaption').textContent = img.alt.replace(/^Photo of /, '');
    dlg.showModal();
  }
  document.addEventListener('click', function (e) {
    var img = e.target.closest('img.zoom');
    if (img) { e.preventDefault(); open(img); }
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('img.zoom')) { e.preventDefault(); open(e.target); }
  });
})();
