import type { CapacitorConfig } from '@capacitor/cli';

// Pawcorder is a self-hosted admin running on the user's home network
// (or Tailscale tailnet). The native shell loads /www/index.html on
// first launch — the user pastes their admin URL once, we persist
// it, and every subsequent launch loads the admin directly.
//
// We deliberately don't bake an admin URL here: this is one app
// binary serving every install on the App Store / Play Store. The
// URL the user types is stored via Capacitor Preferences (encrypted
// at rest by the OS keystore on both platforms).

const config: CapacitorConfig = {
  appId: 'app.pawcorder.shell',
  appName: 'Pawcorder',
  webDir: 'www',
  bundledWebRuntime: false,

  // App Transport Security: the user's admin will often live behind
  // self-signed TLS (Tailscale serves valid certs, but a bare LAN
  // install on http://10.0.0.50 is common). We let the webview load
  // arbitrary http(s) so the app works against any Pawcorder install.
  server: {
    androidScheme: 'https',
    cleartext: true,
  },

  ios: {
    contentInset: 'always',
  },

  android: {
    // Tailscale-served (https) admin embedding LAN-only (http) sub-
    // resources (e.g. the Frigate iframe on the camera page). Without
    // allowMixedContent, https://*.ts.net cannot frame http://10.0.0.50:5000.
    allowMixedContent: true,
    backgroundColor: '#FBF8F3',
  },

  plugins: {
    PushNotifications: {
      // iOS-only field. Android channels (importance, sound, vibration)
      // are created at runtime in src/bootstrap.js via createChannel()
      // since the @capacitor/push-notifications plugin doesn't accept
      // them statically here.
      presentationOptions: ['badge', 'sound', 'alert'],
    },
    LocalNotifications: {
      // Android adaptive smallIcon — single-colour transparent PNG that
      // Android tints with iconColor. Resource path:
      // android/app/src/main/res/drawable/ic_stat_pawcorder.png
      // (generated at `npx cap add android` time from the master SVG).
      smallIcon: 'ic_stat_pawcorder',
      iconColor: '#f37416',
    },
  },
};

export default config;
