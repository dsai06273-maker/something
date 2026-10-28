/* ═══════════════════════════════════════════════════════════════
   NAV TABS — with ARIA tablist keyboard navigation (WCAG 2.1 §2.1.1)
   ═══════════════════════════════════════════════════════════════ */
import { state } from './state.js';
import { loadFeedHistory } from './feed.js';
import { loadApplications } from './applications.js';
import { loadProfileFiles } from './profile.js';
import { loadAnalytics } from './analytics.js';
import { loadResumes } from './resumes.js';
import { loadKnowledgeBase } from './knowledge-base.js';
import { loadSettings, loadApplyMode, loadDefaultResume } from './settings.js';

export function initNavTabs() {
  const tabs = document.querySelectorAll('#navbar .nav-tabs a[role="tab"]');
  tabs.forEach(a => {
    a.addEventListener('click', () => {
      switchScreen(a.dataset.screen, a.dataset.navKey || a.dataset.screen, a.dataset.focusTarget);
    });
    a.addEventListener('keydown', e => {
      const tabArr = [...tabs];
      const idx = tabArr.indexOf(a);
      let newIdx = -1;
      if (e.key === 'ArrowRight') newIdx = (idx + 1) % tabArr.length;
      else if (e.key === 'ArrowLeft') newIdx = (idx - 1 + tabArr.length) % tabArr.length;
      else if (e.key === 'Home') newIdx = 0;
      else if (e.key === 'End') newIdx = tabArr.length - 1;
      if (newIdx >= 0) {
        e.preventDefault();
        tabArr[newIdx].focus();
        const tab = tabArr[newIdx];
        switchScreen(tab.dataset.screen, tab.dataset.navKey || tab.dataset.screen, tab.dataset.focusTarget);
      }
    });
  });
}

export function switchScreen(name, navKey = name, focusTarget = null) {
  state.currentScreen = name;
  state.currentNavKey = navKey;
  document.querySelectorAll('#navbar .nav-tabs a[role="tab"]').forEach(a => {
    const isActive = (a.dataset.navKey || a.dataset.screen) === navKey;
    a.classList.toggle('active', isActive);
    a.setAttribute('aria-selected', isActive ? 'true' : 'false');
    a.tabIndex = isActive ? 0 : -1;
  });
  document.querySelectorAll('.screen').forEach(s => s.classList.add('hidden'));
  const el = document.getElementById('screen-' + name);
  if (el) el.classList.remove('hidden');

  // Load data for the screen
  if (name === 'dashboard') { loadFeedHistory(); loadApplyMode(); loadDefaultResume(); }
  if (name === 'applications') loadApplications();
  if (name === 'profile') loadProfileFiles();
  if (name === 'analytics') loadAnalytics();
  if (name === 'resumes') loadResumes();
  if (name === 'knowledge-base') loadKnowledgeBase();
  if (name === 'settings') loadSettings();

  if (focusTarget) {
    requestAnimationFrame(() => {
      const target = document.getElementById(focusTarget);
      if (!target) return;
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      target.focus({ preventScroll: true });
    });
  }
}

export function showApp() {
  document.getElementById('wizard-overlay').classList.add('hidden');
  document.getElementById('navbar').classList.remove('hidden');
  document.getElementById('app-screens').classList.remove('hidden');
  switchScreen('dashboard');
}
