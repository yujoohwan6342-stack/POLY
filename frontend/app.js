// STREAK SPA — minimal vanilla JS
(() => {
'use strict';

// ─── State ───────────────────────────────────────────────────────
const state = {
  lang: localStorage.getItem('streak_lang') || (navigator.language.startsWith('ko') ? 'ko' : navigator.language.startsWith('zh') ? 'zh' : 'en'),
  i18n: {},
  token: localStorage.getItem('streak_jwt') || null,
  user: null,           // { address, referral_code, tokens, locale }
  page: 'home',
  config: null,         // public config from /api/config
  refFromUrl: new URLSearchParams(location.search).get('ref'),
};

// ─── i18n ────────────────────────────────────────────────────────
async function loadI18n(lang) {
  const r = await fetch(`/static/i18n/${lang}.json`);
  state.i18n = await r.json();
  state.lang = lang;
  localStorage.setItem('streak_lang', lang);
  document.documentElement.lang = lang;
  applyI18n();
}

function t(key, vars = {}) {
  const path = key.split('.');
  let v = state.i18n;
  for (const p of path) v = v?.[p];
  if (typeof v !== 'string') return key;
  return v.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');
}

function applyI18n() {
  document.querySelectorAll('[data-i]').forEach(el => {
    el.textContent = t(el.dataset.i);
  });
  document.querySelectorAll('#lang-switch button').forEach(b => {
    b.classList.toggle('active', b.dataset.lang === state.lang);
  });
}

// ─── API ─────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) { logout(); throw new Error('unauthorized'); }
  if (!r.ok) {
    const txt = await r.text();
    throw new Error(`HTTP ${r.status}: ${txt}`);
  }
  return r.json();
}

// ─── MetaMask + SIWE ─────────────────────────────────────────────
async function ensurePolygon() {
  if (!window.ethereum) throw new Error('no_metamask');
  const chainId = await ethereum.request({ method: 'eth_chainId' });
  if (chainId !== '0x89') {
    try {
      await ethereum.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: '0x89' }] });
    } catch (e) {
      if (e.code === 4902) {
        await ethereum.request({
          method: 'wallet_addEthereumChain',
          params: [{
            chainId: '0x89', chainName: 'Polygon',
            nativeCurrency: { name: 'MATIC', symbol: 'MATIC', decimals: 18 },
            rpcUrls: ['https://polygon-rpc.com'],
            blockExplorerUrls: ['https://polygonscan.com'],
          }],
        });
      } else throw e;
    }
  }
}

async function connectWallet() {
  if (!window.ethereum) {
    showToast(t('auth.no_metamask'));
    window.open('https://metamask.io/download/', '_blank');
    return;
  }
  try {
    await ensurePolygon();
    const [address] = await ethereum.request({ method: 'eth_requestAccounts' });
    const nonceData = await api('/api/auth/nonce');
    const issued = nonceData.issued_at;
    const message =
      `${nonceData.domain} wants you to sign in with your Ethereum account:\n` +
      `${address}\n\n` +
      `Sign in to STREAK\n\n` +
      `URI: ${nonceData.uri}\n` +
      `Version: 1\n` +
      `Chain ID: ${nonceData.chain_id}\n` +
      `Nonce: ${nonceData.nonce}\n` +
      `Issued At: ${issued}`;
    const signature = await ethereum.request({
      method: 'personal_sign', params: [message, address]
    });
    const session = await api('/api/auth/verify', {
      method: 'POST',
      body: JSON.stringify({
        message, signature,
        referral_code: state.refFromUrl,
      }),
    });
    state.token = session.token;
    state.user = session;
    localStorage.setItem('streak_jwt', state.token);
    if (state.refFromUrl) {
      // 1회만 사용
      const url = new URL(location.href);
      url.searchParams.delete('ref');
      history.replaceState({}, '', url);
      state.refFromUrl = null;
    }
    render();
  } catch (e) {
    console.error(e);
    showToast(e.message || t('common.error'));
  }
}

function logout() {
  state.token = null;
  state.user = null;
  localStorage.removeItem('streak_jwt');
  render();
}

// ─── UI helpers ──────────────────────────────────────────────────
function showToast(msg, ms = 1800) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => t.classList.remove('show'), ms);
}

function shortAddr(a) { return a.slice(0, 6) + '…' + a.slice(-4); }
function copy(text, msg) {
  navigator.clipboard.writeText(text);
  showToast(msg || t('wallet.copied'));
}

// ─── Pages ───────────────────────────────────────────────────────
async function pageHome() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <section>
      <div class="card hero">
        <div class="label">${t('home.balance_label')}</div>
        <div class="value">${state.user.tokens.toLocaleString()}</div>
        <div class="sub">${t('home.cycles_left', { n: Math.floor(state.user.tokens / (state.config?.cost_per_cycle || 1)) })}</div>
      </div>
    </section>
    <section class="grid-2">
      <button class="btn ghost" id="btn-topup">💳 ${t('home.topup')}</button>
      <button class="btn ghost" id="btn-invite">🎁 ${t('home.invite')}</button>
    </section>
    <section>
      <div class="card">
        <h3>${t('deploy.title')}</h3>
        <p>${t('deploy.description')}</p>
        <pre>curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/bot/deploy.sh | bash</pre>
        <a href="https://github.com/yujoohwan6342-stack/POLY" target="_blank">${t('deploy.more_info')} →</a>
      </div>
    </section>
  `;
  document.getElementById('btn-topup').onclick = () => navigate('wallet');
  document.getElementById('btn-invite').onclick = () => navigate('referrals');
}

async function pageWallet() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <h1>${t('wallet.title')}</h1>
    <section class="card hero">
      <div class="label">${t('wallet.balance')}</div>
      <div class="value">${state.user.tokens.toLocaleString()}</div>
      <div class="sub">${state.user.tokens} ${t('common.tokens')}</div>
    </section>
    <section class="card">
      <div class="label">${t('wallet.your_address')}</div>
      <div class="row between">
        <span class="addr-pill">${shortAddr(state.user.address)}</span>
        <button class="btn sm ghost" id="btn-copy-addr">${t('wallet.copy')}</button>
      </div>
    </section>

    <h2 style="margin-top:24px;">${t('wallet.earn_title')}</h2>
    <section class="card">
      <h3 style="color:var(--text);">🎁 ${t('wallet.earn_invite')}</h3>
      <p>${t('wallet.earn_invite_desc', { tokens: state.config.ref_l1 })}</p>
      <button class="btn ghost" id="btn-go-ref">→ ${t('referrals.title')}</button>
    </section>
    <section class="card">
      <h3 style="color:var(--text);">📺 ${t('wallet.earn_ads')}</h3>
      <p style="color:var(--text-3);">${t('wallet.earn_ads_desc')}</p>
    </section>

    <section class="card">
      <h2>${t('wallet.history')}</h2>
      <div id="tx-list" class="tx-list"><div class="empty">${t('common.loading')}</div></div>
    </section>
  `;
  document.getElementById('btn-copy-addr').onclick = () => copy(state.user.address);
  document.getElementById('btn-go-ref').onclick = () => navigate('referrals');

  const list = document.getElementById('tx-list');
  try {
    const rows = await api('/api/tokens/history?limit=30');
    if (rows.length === 0) {
      list.innerHTML = `<div class="empty">${t('wallet.no_history')}</div>`;
    } else {
      list.innerHTML = rows.map(tx => {
        const cls = tx.delta > 0 ? 'pos' : 'neg';
        const sign = tx.delta > 0 ? '+' : '';
        const ts = new Date(tx.created_at).toLocaleString();
        return `
          <div class="tx-item">
            <div>
              <div>${tx.kind}</div>
              <div class="meta">${ts}${tx.note ? ' · ' + tx.note : ''}</div>
            </div>
            <div class="tx-amount ${cls}">${sign}${tx.delta}</div>
          </div>`;
      }).join('');
    }
  } catch (e) { list.innerHTML = `<div class="empty">${t('common.error')}</div>`; }
}

function renderTree(node, depth = 0) {
  const indent = '  '.repeat(depth);
  const me = depth === 0 ? '🟢' : '└─';
  let out = `${indent}${me} ${node.short}  ·  ${node.referral_code}  ·  ${node.joined}\n`;
  for (const child of (node.children || [])) {
    out += renderTree(child, depth + 1);
  }
  return out;
}

async function pageReferrals() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <h1>${t('referrals.title')}</h1>
    <section class="card">
      <div class="label">${t('referrals.your_code')}</div>
      <div class="row between" style="margin-top:6px;">
        <div class="value sm">${state.user.referral_code}</div>
        <button class="btn sm ghost" id="btn-copy-code">${t('wallet.copy')}</button>
      </div>
    </section>
    <section class="card">
      <div class="label">${t('referrals.your_link')}</div>
      <code id="invite-link" style="display:block;margin:6px 0;font-size:11px;word-break:break-all;"></code>
      <button class="btn" id="btn-copy-link">${t('referrals.copy_link')}</button>
    </section>
    <section class="card">
      <h3 style="color:var(--text);">${t('referrals.earn_l1', { tokens: state.config.ref_l1 })}</h3>
      <h3 style="color:var(--text);">${t('referrals.earn_l2', { tokens: state.config.ref_l2 })}</h3>
    </section>
    <section class="grid-2">
      <div class="card">
        <div class="label">${t('referrals.stats_direct')}</div>
        <div class="value sm" id="stat-direct">—</div>
      </div>
      <div class="card">
        <div class="label">${t('referrals.stats_indirect')}</div>
        <div class="value sm" id="stat-indirect">—</div>
      </div>
    </section>
    <section class="card">
      <div class="label">${t('referrals.stats_earned')}</div>
      <div class="value sm" id="stat-earned">—</div>
    </section>
    <section class="card">
      <h2>${t('referrals.tree_title')}</h2>
      <div id="tree-area"></div>
    </section>
  `;

  const stats = await api('/api/referrals/stats');
  document.getElementById('invite-link').textContent = stats.invite_url;
  document.getElementById('stat-direct').textContent = stats.direct_count;
  document.getElementById('stat-indirect').textContent = stats.indirect_count;
  document.getElementById('stat-earned').textContent = stats.tokens_earned + ' ' + t('common.tokens');

  document.getElementById('btn-copy-code').onclick = () => copy(state.user.referral_code);
  document.getElementById('btn-copy-link').onclick = () => copy(stats.invite_url);

  const tree = await api('/api/referrals/tree');
  const area = document.getElementById('tree-area');
  if (!tree.children || tree.children.length === 0) {
    area.innerHTML = `<div class="empty">${t('referrals.no_referrals')}</div>`;
  } else {
    area.innerHTML = `<div class="tree">${renderTree(tree)}</div>`;
  }
}

function pageSettings() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <h1>${t('settings.title')}</h1>
    <section class="card">
      <div class="label">${t('settings.language')}</div>
      <div class="row" style="margin-top:8px; gap: 8px;">
        ${['en','ko','zh'].map(l => `<button class="btn sm ${state.lang===l?'':'ghost'}" data-lang="${l}">${l.toUpperCase()}</button>`).join('')}
      </div>
    </section>
    <section class="card">
      <h3 style="color:var(--neg);">${t('settings.danger_zone')}</h3>
      <button class="btn danger" id="btn-logout">${t('settings.disconnect')}</button>
    </section>
  `;
  main.querySelectorAll('[data-lang]').forEach(b => {
    b.onclick = async () => { await loadI18n(b.dataset.lang); pageSettings(); };
  });
  document.getElementById('btn-logout').onclick = () => logout();
}

// ─── Routing ─────────────────────────────────────────────────────
function navigate(page) {
  state.page = page;
  document.querySelectorAll('#bottom-nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.page === page);
  });
  render();
}

function render() {
  if (!state.token || !state.user) {
    document.getElementById('bottom-nav').style.display = 'none';
    renderConnectScreen();
    return;
  }
  document.getElementById('bottom-nav').style.display = 'flex';
  applyI18n();
  switch (state.page) {
    case 'home': pageHome(); break;
    case 'wallet': pageWallet(); break;
    case 'referrals': pageReferrals(); break;
    case 'settings': pageSettings(); break;
  }
}

function renderConnectScreen() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <div class="connect-screen">
      <div class="logo">STREAK</div>
      <div class="tagline">${t('tagline')}</div>
      <div class="counter" style="margin: 32px 0;">
        <div class="label"><span class="live-dot"></span>${t('home_counter.label')}</div>
        <div class="number" id="counter-num">0</div>
        <div class="subtitle">${t('home_counter.subtitle')}</div>
      </div>
      <button class="btn" id="btn-connect">🦊 ${t('auth.connect')}</button>
      <p style="margin-top:24px; font-size:12px; color:var(--text-3);">${t('auth.by_signing')}</p>
    </div>
  `;
  document.getElementById('btn-connect').onclick = connectWallet;
  startCounter();
}

// 누적 가입자 카운터 — 자동 증가 애니메이션 + 폴링
let _counterTimer = null;
let _counterTarget = 0;
let _counterDisplay = 0;

function startCounter() {
  if (_counterTimer) clearInterval(_counterTimer);

  async function fetchTotal() {
    try {
      const r = await fetch('/api/stats/public').then(x => x.json());
      _counterTarget = r.total_users || 0;
    } catch (e) {}
  }
  fetchTotal();
  setInterval(fetchTotal, 10000);  // 10초마다 폴링

  // count-up animation @ 60fps
  _counterTimer = setInterval(() => {
    const el = document.getElementById('counter-num');
    if (!el) { clearInterval(_counterTimer); return; }
    if (_counterDisplay < _counterTarget) {
      const diff = _counterTarget - _counterDisplay;
      const step = Math.max(1, Math.ceil(diff / 30));
      _counterDisplay += step;
      if (_counterDisplay > _counterTarget) _counterDisplay = _counterTarget;
      el.textContent = _counterDisplay.toLocaleString();
      el.classList.add('bump');
      setTimeout(() => el.classList.remove('bump'), 400);
    }
  }, 60);
}

// ─── Init ────────────────────────────────────────────────────────
async function init() {
  await loadI18n(state.lang);
  state.config = await api('/api/config');

  document.querySelectorAll('#lang-switch button').forEach(b => {
    b.onclick = () => loadI18n(b.dataset.lang).then(render);
  });
  document.querySelectorAll('#bottom-nav a').forEach(a => {
    a.onclick = (e) => { e.preventDefault(); navigate(a.dataset.page); };
  });

  if (state.token) {
    try {
      const me = await api('/api/auth/me');
      state.user = { address: me.address, referral_code: me.referral_code, tokens: me.tokens, locale: me.locale };
    } catch { state.token = null; localStorage.removeItem('streak_jwt'); }
  }
  render();
}

init().catch(e => { console.error(e); showToast(t('common.error')); });
})();
