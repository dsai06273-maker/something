/* ═══════════════════════════════════════════════════════════════
   EMPLOYER EMAIL MONITORING — settings panel, suggestions, live updates
   ═══════════════════════════════════════════════════════════════ */
import { t } from './i18n.js';
import { escHtml } from './helpers.js';

const $ = (id) => document.getElementById(id);
const JSON_HEADERS = { 'Content-Type': 'application/json' };

function setFeedback(text, kind = 'dim') {
  const el = $('email-feedback');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = kind === 'error' ? 'var(--danger)' : kind === 'ok' ? 'var(--success)' : 'var(--text-dim)';
}

function setBusy(id, busy, busyKey, idleKey) {
  const btn = $(id);
  if (!btn) return;
  btn.disabled = busy;
  btn.textContent = t(busy ? busyKey : idleKey);
}

async function errorText(res) {
  try { return (await res.json()).error || res.statusText; } catch { return res.statusText; }
}

export function onEmailProviderChange() {
  const custom = $('set-email-provider').value === 'custom';
  $('email-custom-host-row').classList.toggle('hidden', !custom);
}

function renderStatus(cfg) {
  const st = cfg.status || {};
  $('email-status-badge').classList.toggle('hidden', !st.running);
  const when = st.last_check_at ? new Date(st.last_check_at).toLocaleString() : t('email.never');
  $('email-last-check').textContent = t('email.last_check', { time: when });
  if (st.last_error) setFeedback(st.last_error, 'error');
}

export async function loadEmailSettings() {
  if (!$('set-email-enabled')) return;
  try {
    const res = await fetch('/api/email/config');
    if (!res.ok) return;
    const cfg = await res.json();
    $('set-email-enabled').checked = !!cfg.enabled;
    $('set-email-provider').value = cfg.provider || 'gmail';
    $('set-email-username').value = cfg.username || '';
    $('set-email-host').value = cfg.imap_host || '';
    $('set-email-port').value = cfg.imap_port || 993;
    $('set-email-folder').value = cfg.folder || 'INBOX';
    $('set-email-interval').value = cfg.poll_interval_minutes || 15;
    $('set-email-lookback').value = cfg.lookback_days || 14;
    $('set-email-auto').checked = cfg.auto_update_status !== false;
    $('set-email-password').value = '';
    $('set-email-password').placeholder = cfg.has_password ? '••••••••' : '';
    $('email-password-hint').textContent = cfg.has_password ? t('email.password_saved') : t('email.app_password_hint');
    onEmailProviderChange();
    renderStatus(cfg);
    await loadEmailSuggestions();
  } catch { /* settings page still usable without email */ }
}

function formPayload() {
  const body = {
    enabled: $('set-email-enabled').checked,
    provider: $('set-email-provider').value,
    username: $('set-email-username').value.trim(),
    imap_host: $('set-email-host').value.trim(),
    imap_port: parseInt($('set-email-port').value, 10) || 993,
    folder: $('set-email-folder').value.trim() || 'INBOX',
    poll_interval_minutes: parseInt($('set-email-interval').value, 10) || 15,
    lookback_days: parseInt($('set-email-lookback').value, 10) || 14,
    auto_update_status: $('set-email-auto').checked,
  };
  const pw = $('set-email-password').value;
  if (pw) body.password = pw;
  return body;
}

export async function saveEmailSettings() {
  setFeedback('');
  try {
    const res = await fetch('/api/email/config', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(formPayload()) });
    if (!res.ok) { setFeedback(await errorText(res), 'error'); return; }
    setFeedback(t('email.saved'), 'ok');
    await loadEmailSettings();
  } catch {
    setFeedback(t('email.save_error'), 'error');
  }
}

export async function testEmailConnection() {
  setFeedback('');
  setBusy('email-test-btn', true, 'email.testing', 'email.test');
  try {
    const res = await fetch('/api/email/test', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(formPayload()) });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) setFeedback(t('email.test_ok', { count: data.message_count }), 'ok');
    else setFeedback(data.error || res.statusText, 'error');
  } catch (e) {
    setFeedback(String(e.message || e), 'error');
  } finally {
    setBusy('email-test-btn', false, 'email.testing', 'email.test');
  }
}

export async function checkEmailNow() {
  setFeedback('');
  setBusy('email-check-btn', true, 'email.checking', 'email.check_now');
  try {
    const res = await fetch('/api/email/check', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { setFeedback(data.error || res.statusText, 'error'); return; }
    setFeedback(t('email.check_result', data), 'ok');
    await loadEmailSettings();
  } catch (e) {
    setFeedback(String(e.message || e), 'error');
  } finally {
    setBusy('email-check-btn', false, 'email.checking', 'email.check_now');
  }
}

export async function loadEmailSuggestions() {
  const box = $('email-suggestions');
  if (!box) return;
  try {
    const res = await fetch('/api/email/messages?action=suggested');
    if (!res.ok) return;
    const rows = await res.json();
    if (!rows.length) {
      box.innerHTML = `<div class="text-dim">${escHtml(t('email.no_suggestions'))}</div>`;
      return;
    }
    box.innerHTML = rows.map((m) => `
      <div style="display:flex; gap:8px; align-items:center; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border);">
        <div>
          <div style="font-weight:600;">${escHtml(t('email.suggestion_line', {
            company: m.company || '?', title: m.job_title || '', status: t('status.' + m.proposed_status),
          }))}</div>
          <div class="text-dim" style="font-size:.8rem;">${escHtml(m.subject || '')}</div>
        </div>
        <div style="display:flex; gap:6px; flex-shrink:0;">
          <button type="button" class="btn btn-primary btn-sm" data-email-action="apply" data-email-id="${m.id}">${escHtml(t('email.accept'))}</button>
          <button type="button" class="btn btn-ghost btn-sm" data-email-action="dismiss" data-email-id="${m.id}">${escHtml(t('email.dismiss'))}</button>
        </div>
      </div>`).join('');
  } catch { /* non-critical */ }
}

export async function resolveSuggestion(id, action) {
  try {
    const res = await fetch(`/api/email/messages/${id}/${action}`, { method: 'POST' });
    if (!res.ok) setFeedback(await errorText(res), 'error');
  } catch (e) {
    setFeedback(String(e.message || e), 'error');
  }
  await loadEmailSuggestions();
  if (typeof window.loadApplications === 'function') window.loadApplications();
}

/** Small accessible toast; no dependency on any other UI module. */
function toast(text) {
  const el = document.createElement('div');
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.textContent = text;
  el.style.cssText = 'position:fixed; right:16px; bottom:16px; z-index:9999; max-width:360px; padding:10px 14px;'
    + ' background:var(--panel); color:var(--text-bright); border:1px solid var(--border); border-left:4px solid var(--success);'
    + ' border-radius:var(--radius-sm); box-shadow:var(--shadow); font-size:.88rem;';
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

/** Socket handler for server-pushed `application_status_changed` events. */
export async function handleStatusChanged(evt) {
  try {
    const res = await fetch(`/api/applications/${evt.application_id}`);
    const app = res.ok ? await res.json() : {};
    toast(t('email.toast_updated', { company: app.company || '', status: t('status.' + evt.status) }));
  } catch {
    toast(t('email.toast_updated', { company: '', status: t('status.' + evt.status) }));
  }
  if (typeof window.loadApplications === 'function') window.loadApplications();
  loadEmailSuggestions();
}

/** Delegated click handling for suggestion buttons (no inline handlers on dynamic HTML). */
export function initEmailMonitor() {
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-email-action]');
    if (btn) resolveSuggestion(btn.dataset.emailId, btn.dataset.emailAction);
  });
}
