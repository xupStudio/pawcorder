/* pawcorder native shell — first-run bootstrap.
 *
 * Flow:
 *   1. On launch, check Capacitor Preferences for a saved admin URL.
 *   2. If missing → show the connect form (index.html). User pastes,
 *      we sanity-check it, save, and `location.replace` to it.
 *   3. If present → register for push (best-effort), then redirect
 *      to the admin URL. The webview takes over from there.
 *
 * Push registration:
 *   - Asks for permission ONCE (idempotent on iOS; subsequent calls
 *     just re-deliver the existing token).
 *   - POSTs the token to <admin>/api/webpush/native so the admin
 *     adds it to its push targets list. The admin already supports
 *     VAPID Web Push; native APNs is an additional channel routed
 *     by the same notification dispatcher.
 *
 * If anything fails (no network, admin unreachable) we still load
 * the admin — push will register on next launch.
 */

import { Capacitor } from '@capacitor/core';
import { Preferences } from '@capacitor/preferences';
import { PushNotifications } from '@capacitor/push-notifications';

const KEY_ADMIN_URL = 'pawcorder.adminUrl';
const KEY_PUSH_REGISTERED = 'pawcorder.pushRegistered';

function showError(msg) {
  const el = document.getElementById('err');
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
}

// Schemes other than http/https are blocked outright. Without this,
// pasting `javascript:fetch(...)` would land in Preferences and run on
// the next launch's `location.replace` (in the webview's privileged
// origin). The /^[a-z][a-z0-9+\-.]*:/ heuristic catches any URI scheme
// (including data:, file:, javascript:) so we can reject before
// auto-prefixing http:// to bare hosts.
const _SCHEME_RE = /^[a-z][a-z0-9+\-.]*:/i;

function normaliseUrl(raw) {
  const trimmed = (raw || '').trim();
  if (!trimmed) return null;
  if (_SCHEME_RE.test(trimmed)) {
    // Has a scheme — must be http or https.
    if (!/^https?:\/\//i.test(trimmed)) return null;
    return trimmed.replace(/\/+$/, '');
  }
  // Bare host:port — auto-prefix http://.
  return 'http://' + trimmed.replace(/\/+$/, '');
}

async function probeAdmin(url) {
  // Cheap reachability check — admin's /login is anonymous and
  // returns HTML on every install. Network errors / 5xx → return false
  // so the user gets a clear "couldn't reach it" message.
  try {
    const r = await fetch(url + '/login', { method: 'GET',
                                              cache: 'no-store',
                                              credentials: 'omit' });
    return r.ok;
  } catch {
    return false;
  }
}

async function registerPushIfNeeded(adminUrl) {
  // Only on real devices; the simulator doesn't have an APNs token
  // and would fail silently otherwise.
  if (!Capacitor.isNativePlatform()) return;
  const { value: already } = await Preferences.get({ key: KEY_PUSH_REGISTERED });
  if (already === '1') return;

  const perm = await PushNotifications.requestPermissions();
  if (perm.receive !== 'granted') return;

  // Android 8+ requires every notification to land in a channel.
  // Importance 4 = HIGH = heads-up alert (the popup style users expect
  // for "your pet was detected" events). Without an explicit channel
  // FCM falls back to the Firebase default which is silent on many OEMs.
  if (Capacitor.getPlatform() === 'android') {
    try {
      await PushNotifications.createChannel({
        id: 'pawcorder-events',
        name: 'Pet detections',
        description: 'Live alerts when your camera sees a pet event',
        importance: 4,
        sound: 'default',
        vibration: true,
        lights: true,
        lightColor: '#f37416',
      });
    } catch (e) {
      console.warn('createChannel failed', e);
    }
  }

  await PushNotifications.register();
  PushNotifications.addListener('registration', async (token) => {
    try {
      // Admin adds the APNs/FCM token alongside its existing VAPID
      // subscriptions. Same auth (cookie) — the user logged in via
      // the webview before this call, so the cookie is already set.
      await fetch(adminUrl + '/api/webpush/native', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'pawcorder',
        },
        body: JSON.stringify({
          token: token.value,
          platform: Capacitor.getPlatform(),  // 'ios' | 'android'
        }),
      });
      await Preferences.set({ key: KEY_PUSH_REGISTERED, value: '1' });
    } catch (e) {
      // Best-effort — we'll retry on next launch.
      console.warn('push register failed', e);
    }
  });
}

async function main() {
  const { value: savedUrl } = await Preferences.get({ key: KEY_ADMIN_URL });
  // Re-validate on every launch — defense in depth against a stored
  // value that survived from an older binary that didn't yet enforce
  // the scheme allowlist. Route through normaliseUrl so the same
  // rules that gate user input also gate the persisted value (no
  // possibility of a stricter check on save vs. load).
  const validated = normaliseUrl(savedUrl);
  if (validated) {
    // Returning launch — kick off push registration in the background
    // and redirect to the admin. Don't await register: a slow APNs
    // token shouldn't block the redirect.
    registerPushIfNeeded(validated);
    location.replace(validated);
    return;
  }
  if (savedUrl) {
    // Stored value failed validation — drop it.
    await Preferences.remove({ key: KEY_ADMIN_URL });
  }

  const input  = document.getElementById('adminUrl');
  const button = document.getElementById('connect');

  input.addEventListener('input', () => {
    button.disabled = !input.value.trim();
  });

  button.addEventListener('click', async () => {
    button.disabled = true;
    const url = normaliseUrl(input.value);
    if (!url) {
      showError('Please enter an http:// or https:// URL.');
      button.disabled = false;
      return;
    }
    const reachable = await probeAdmin(url);
    if (!reachable) {
      showError('Could not reach that URL. Check the address and try again.');
      button.disabled = false;
      return;
    }
    await Preferences.set({ key: KEY_ADMIN_URL, value: url });
    registerPushIfNeeded(url);
    location.replace(url);
  });
}

main().catch((e) => {
  console.error(e);
  showError('Something went wrong: ' + (e?.message || e));
});
