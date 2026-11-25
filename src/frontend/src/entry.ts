const kefuPath = `${import.meta.env.BASE_URL.replace(/\/$/, '')}/kefu`

// Framework7 sets the theme on <html>, so it runs in its own document entry.
if (window.location.pathname.replace(/\/$/, '').toLowerCase() === kefuPath.toLowerCase()) {
  void import('./views/kefu/main')
} else {
  void import('./main')
}
