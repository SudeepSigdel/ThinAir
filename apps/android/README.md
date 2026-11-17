# Android app (Trusted Web Activity), not built yet

Thinair is a PWA: "Add to Home Screen" gives an app that runs fullscreen, with notifications (Web Push) and
a share target. A Play Store wrapper is optional and was left out on purpose, because it needs things only
the owner can provide:

- a **signing key** and a Play developer account;
- a **Digital Asset Links** file served from your server at `/.well-known/assetlinks.json`, naming the app's
  package and the key's SHA-256 (without it the wrapper shows a browser address bar);
- one wrapper per server address (the address is baked into the app).

If you want one, [Bubblewrap](https://github.com/GoogleChromeLabs/bubblewrap) generates it from the web
manifest (`/manifest.webmanifest`, which already has an `id`, icons and a `share_target`):

```sh
npm i -g @bubblewrap/cli
bubblewrap init --manifest https://YOUR-ADDRESS/manifest.webmanifest
bubblewrap build
```

Nothing here is tested. The PWA is the supported way to install Thinair on Android.
