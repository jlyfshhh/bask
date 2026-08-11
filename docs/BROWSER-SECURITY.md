# Browser security and installation

Bask is designed to stay on a trusted home network. It does not need to be,
and should not be, forwarded directly to the public internet.

## HTTP and the installable app

Bask works as a normal website over a local HTTP address. Browser security
rules are stricter for an installed Progressive Web App:

- Service workers require a **secure context**. Browsers treat HTTPS and the
  same-device `localhost` exception as secure, but ordinarily do not treat
  `http://192.168.x.x` or an HTTP `.local` hostname as secure.
- On plain LAN HTTP, a browser may still offer **Add to Home Screen** as a
  bookmark or standalone-looking shortcut. Do not rely on service-worker
  caching, offline startup, or the full install prompt in that configuration.
- For consistent PWA behavior, put Bask behind a trusted HTTPS reverse proxy
  on the LAN. Keep Bask's Head Keeper key enabled and keep the host firewalled;
  HTTPS is not a reason to expose the service publicly.

The dashboard intentionally remains useful without PWA installation, so a
plain HTTP deployment is a supported local-network setup.

## Response policy

Every Bask response carries a Content Security Policy, MIME-sniffing
protection, frame denial, a no-referrer policy, and a restrictive browser
permissions policy. FastAPI's interactive documentation and OpenAPI schema are
disabled in the production application, and the production web command omits
Uvicorn's identifying `Server` header.

The current no-build frontend creates controls with inline event-handler
attributes. Its CSP therefore still needs `script-src 'unsafe-inline'` for
compatibility. Scripts and network connections remain limited to Bask's own
origin, plugins are disabled, and framing is denied. Removing inline handlers
would allow that remaining CSP exception to be removed in a future frontend
refactor.
