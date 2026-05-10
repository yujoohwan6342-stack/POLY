// STREAK SPA — Firebase Anonymous + Google auth
(() => {
'use strict';

const state = {
  lang: localStorage.getItem('streak_lang') ||
        (navigator.language.startsWith('ko') ? 'ko' :
         navigator.language.startsWith('zh') ? 'zh' : 'en'),
  i18n: {},
  user: null,            // { address: firebase_uid, referral_code, tokens, locale, auth_method, email }
  page: 'home',
  config: null,
  refFromUrl: new URLSearchParams(location.search).get('ref'),
  fbApp: null, fbAuth: null, fbUser: null,
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
  document.querySelectorAll('[data-i]').forEach(el => { el.textContent = t(el.dataset.i); });
  document.querySelectorAll('#lang-switch button').forEach(b => {
    b.classList.toggle('active', b.dataset.lang === state.lang);
  });
}

// ─── API ─────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (state.fbUser) {
    const tok = await state.fbUser.getIdToken();
    headers['Authorization'] = `Bearer ${tok}`;
  }
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    const txt = await r.text();
    throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status, body: txt });
  }
  return r.json();
}

// ─── Firebase init ───────────────────────────────────────────────
function waitForFirebaseSDK() {
  return new Promise(res => {
    if (window.firebaseSDK) return res();
    window.addEventListener('firebase-sdk-ready', res, { once: true });
  });
}

async function initFirebase(cfg) {
  await waitForFirebaseSDK();
  const sdk = window.firebaseSDK;
  if (!cfg.apiKey) {
    showToast('Firebase config not set on server'); return;
  }
  state.fbApp = sdk.initializeApp(cfg);
  state.fbAuth = sdk.getAuth(state.fbApp);

  return new Promise(res => {
    sdk.onAuthStateChanged(state.fbAuth, async (fbUser) => {
      state.fbUser = fbUser;
      if (fbUser) {
        await syncBackendUser();
      } else {
        state.user = null;
      }
      render();
      res();
    });
  });
}

async function syncBackendUser() {
  // /api/auth/me 시도 → 404면 register 호출
  try {
    state.user = await api('/api/auth/me');
  } catch (e) {
    if (e.status === 404) {
      // 신규 가입 (referral 코드 포함)
      try {
        state.user = await api('/api/auth/register', {
          method: 'POST',
          body: JSON.stringify({ referral_code: state.refFromUrl }),
        });
        if (state.refFromUrl) {
          const url = new URL(location.href);
          url.searchParams.delete('ref');
          history.replaceState({}, '', url);
          state.refFromUrl = null;
        }
      } catch (ee) { console.error('register failed', ee); showToast(t('common.error')); }
    } else {
      console.error(e); showToast(t('common.error'));
    }
  }
}

// ─── Auth actions ────────────────────────────────────────────────
async function signInAnon() {
  try {
    await window.firebaseSDK.signInAnonymously(state.fbAuth);
  } catch (e) {
    console.error(e); showToast(e.message || t('common.error'));
  }
}

async function signInGoogle() {
  try {
    const provider = new window.firebaseSDK.GoogleAuthProvider();
    if (state.fbUser && state.fbUser.isAnonymous) {
      // 익명 → Google 업그레이드 (UID 유지 + +10 보너스)
      await window.firebaseSDK.linkWithPopup(state.fbUser, provider);
    } else {
      await window.firebaseSDK.signInWithPopup(state.fbAuth, provider);
    }
  } catch (e) {
    if (e.code === 'auth/credential-already-in-use') {
      // 다른 익명 계정과 이미 연결된 Google 계정 — 그냥 sign in
      try {
        await window.firebaseSDK.signInWithPopup(state.fbAuth, new window.firebaseSDK.GoogleAuthProvider());
      } catch (e2) { showToast(e2.message); }
    } else if (e.code === 'auth/popup-closed-by-user') {
      // user closed popup — silent
    } else {
      console.error(e); showToast(e.message || t('common.error'));
    }
  }
}

async function signOutUser() {
  await window.firebaseSDK.signOut(state.fbAuth);
}

// ─── UI helpers ──────────────────────────────────────────────────
function showToast(msg, ms = 1800) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.remove('show'), ms);
}
function shortAddr(a) { return (a || '').slice(0, 6) + '…' + (a || '').slice(-4); }
function copy(text, msg) { navigator.clipboard.writeText(text); showToast(msg || t('wallet.copied')); }

// ─── Pages ───────────────────────────────────────────────────────
async function pageHome() {
  const main = document.getElementById('main');
  const isAnon = state.user.auth_method === 'anonymous';
  const today = new Date();
  const dateStr = today.toLocaleDateString(state.lang === 'ko' ? 'ko-KR' : state.lang === 'zh' ? 'zh-CN' : 'en-US',
    { year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'long' });

  main.innerHTML = `
    <div class="row between" style="margin-bottom:16px;">
      <div>
        <div class="eyebrow">${dateStr}</div>
        <h1 style="margin-top:4px; margin-bottom:0;">${t('home.greeting')}</h1>
      </div>
    </div>

    <div class="card hero">
      <div class="grid-line"></div>
      <div class="accent-block"></div>
      <div style="position:relative;">
        <div class="label">${t('home.balance_label')}</div>
        <div class="value lg">${state.user.tokens.toLocaleString()}</div>
        <div class="sub">${t('home.cycles_left', { n: state.user.tokens })}</div>
        <div class="row" style="margin-top:18px; gap:8px;">
          <button class="btn white" id="btn-go-trading-2" style="flex:1; padding:11px 14px; font-size:13px;">${t('home.go_trading')}</button>
          <button class="btn outline" id="btn-invite-2" style="flex:1; padding:11px 14px; font-size:13px; background:transparent; color:#fff; border-color:rgba(255,255,255,0.2);">${t('home.invite')}</button>
        </div>
      </div>
    </div>

    ${isAnon ? `
      <div class="nudge">
        <div class="row between" style="align-items:flex-start;">
          <div style="flex:1;">
            <h3 style="margin:0;">${t('home.upgrade_title')}</h3>
            <p style="margin-top:4px;">${t('home.upgrade_desc', { tokens: state.config.upgrade_bonus })}</p>
          </div>
          <span class="reward outline">+${state.config.upgrade_bonus}</span>
        </div>
        <button class="btn" id="btn-upgrade" style="margin-top:12px; padding:10px 14px; font-size:13px;">${t('home.upgrade_cta')}</button>
      </div>
    ` : ''}
  `;
  if (isAnon) document.getElementById('btn-upgrade').onclick = signInGoogle;
  document.getElementById('btn-invite-2').onclick = () => navigate('referrals');
  document.getElementById('btn-go-trading-2').onclick = () => navigate('trading');
}

async function pageWallet() {
  const main = document.getElementById('main');
  const provider = state.user.auth_method === 'google' || state.user.auth_method === 'upgraded' ? 'Google' :
                   state.user.auth_method === 'anonymous' ? 'Guest' : state.user.auth_method;
  main.innerHTML = `
    <h1>${t('wallet.title')}</h1>

    <div class="card hero">
      <div class="grid-line"></div>
      <div class="accent-block"></div>
      <div style="position:relative;">
        <div class="label">${t('wallet.balance')}</div>
        <div class="value lg">${state.user.tokens.toLocaleString()}</div>
        <div class="sub">${t('wallet.balance_hint')}</div>
      </div>
    </div>

    <div class="card tight" style="display:flex; justify-content:space-between; align-items:center;">
      <div>
        <div class="label">${t('wallet.your_address')}</div>
        <div style="font-weight:600; font-size:14px; margin-top:2px;">${state.user.email || shortAddr(state.user.address || '')}</div>
      </div>
      <span class="pill">${provider}</span>
    </div>

    <h2>${t('wallet.earn_title')}</h2>
    <div class="card tight">
      <div class="row between">
        <div>
          <div style="font-weight:600; font-size:14px;">${t('wallet.earn_invite')}</div>
          <div class="muted" style="margin-top:3px;">${t('wallet.earn_invite_desc', { tokens: state.config.ref_l1 })}</div>
        </div>
        <button class="btn sm" id="btn-go-ref">${t('wallet.earn_invite_cta')}</button>
      </div>
    </div>
    <div class="card tight" style="opacity:0.55;">
      <div class="row between">
        <div>
          <div style="font-weight:600; font-size:14px;">${t('wallet.earn_ads')}</div>
          <div class="muted" style="margin-top:3px;">${t('wallet.earn_ads_desc')}</div>
        </div>
        <span class="pill outline">${t('wallet.earn_ads_pill')}</span>
      </div>
    </div>

    <h2>${t('wallet.history')}</h2>
    <div class="card tight">
      <div id="tx-list" class="list"><div class="empty">${t('common.loading')}</div></div>
    </div>
  `;
  document.getElementById('btn-go-ref').onclick = () => navigate('referrals');
  const list = document.getElementById('tx-list');
  try {
    const rows = await api('/api/tokens/history?limit=30');
    if (!rows.length) {
      list.innerHTML = `<div class="empty">${t('wallet.no_history')}</div>`;
    } else {
      list.innerHTML = rows.map(tx => {
        const cls = tx.delta > 0 ? 'pos' : 'neg';
        const sign = tx.delta > 0 ? '+' : '';
        const ts = new Date(tx.created_at).toLocaleString(state.lang === 'ko' ? 'ko-KR' : 'en-US');
        const labelMap = {
          signup_anon: t('wallet.history') + ' · signup',
          signup_google: t('wallet.history') + ' · Google',
          upgrade: 'Google upgrade',
          ref_l1: t('referrals.l1_label'),
          ref_l2: t('referrals.l2_label'),
          referred: t('referrals.title'),
          cycle: 'Trade',
          ad: 'Ad',
        };
        const label = labelMap[tx.kind] || tx.kind;
        return `<div class="list-row">
          <div>
            <div style="font-weight:600; font-size:13px;">${label}</div>
            <div class="meta">${ts}${tx.note ? ' · ' + tx.note : ''}</div>
          </div>
          <div class="amount ${cls} mono">${sign}${tx.delta}</div>
        </div>`;
      }).join('');
    }
  } catch (e) { list.innerHTML = `<div class="empty">${t('common.error')}</div>`; }
}

function renderTree(node, depth = 0) {
  const indent = '  '.repeat(depth);
  const me = depth === 0 ? '🟢' : '└─';
  const tag = node.auth_method === 'google' ? '🌐' : node.auth_method === 'upgraded' ? '✨' : '👤';
  let out = `${indent}${me} ${tag} ${node.short}  ·  ${node.referral_code}  ·  ${node.joined}\n`;
  for (const c of (node.children || [])) out += renderTree(c, depth + 1);
  return out;
}

function renderRefTree(node, depth = 0) {
  if (!node) return '';
  if (depth === 0) {
    let out = `<div class="tree-line"><span class="avatar me">ME</span><span style="font-weight:600; color:var(--text);" class="id">${node.referral_code || ''}</span></div>`;
    for (const c of (node.children || [])) out += renderRefTree(c, 1);
    return out;
  }
  const tag = node.auth_method === 'google' ? 'G' : node.auth_method === 'upgraded' ? '★' : 'A';
  const reward = depth === 1 ? `<span class="reward">+${state.config.ref_l1}</span>` : `<span class="reward outline">+${state.config.ref_l2}</span>`;
  const dateStr = node.joined ? new Date(node.joined).toLocaleDateString(state.lang === 'ko' ? 'ko-KR' : 'en-US', { month: 'short', day: 'numeric' }) : '';
  let out = `<div class="tree-line depth-${depth}">
    <span class="branch">└</span>
    <span class="avatar">${tag}</span>
    <span class="id">${node.short || ''}</span>
    <span class="muted" style="margin-left:auto; margin-right:8px;">${dateStr}</span>
    ${reward}
  </div>`;
  for (const c of (node.children || [])) out += renderRefTree(c, depth + 1);
  return out;
}

async function pageReferrals() {
  const main = document.getElementById('main');
  const canEnterCode = !state.user.referred_by_id;
  main.innerHTML = `
    <h1>${t('referrals.title')}</h1>

    <div class="card hero">
      <div class="grid-line"></div>
      <div style="position:relative;">
        <div class="label">${t('referrals.your_code')}</div>
        <div style="font-size:32px; font-weight:700; letter-spacing:0.06em; margin:6px 0 4px; font-family:'JetBrains Mono','SF Mono', monospace;">${state.user.referral_code}</div>
        <div class="sub">${t('referrals.code_hint')}</div>
        <button class="btn white" id="btn-copy-link" style="margin-top:16px;">${t('referrals.copy_link')}</button>
      </div>
    </div>

    ${canEnterCode ? `
      <div class="nudge">
        <h3 style="margin:0 0 6px;">${t('referrals.enter_code', { tokens: state.config.referred_bonus })}</h3>
        <div class="row" style="gap:6px; margin-top:8px;">
          <input id="ref-code-in" type="text" placeholder="${t('referrals.code_input_ph')}" style="flex:1; font-family:'JetBrains Mono', monospace; text-transform:uppercase;" />
          <button class="btn sm" id="ref-code-apply">${t('referrals.code_apply')}</button>
        </div>
      </div>
    ` : ''}

    <h2>${t('referrals.rewards_title')}</h2>
    <div class="card tight" style="display:flex; align-items:center; gap:14px;">
      <div class="tag-num solid">L1</div>
      <div style="flex:1;">
        <div style="font-weight:600; font-size:14px;">${t('referrals.l1_label')}</div>
        <div class="muted" style="margin-top:2px;">${t('referrals.l1_desc')}</div>
      </div>
      <span class="reward">+${state.config.ref_l1}</span>
    </div>
    <div class="card tight" style="display:flex; align-items:center; gap:14px;">
      <div class="tag-num">L2</div>
      <div style="flex:1;">
        <div style="font-weight:600; font-size:14px;">${t('referrals.l2_label')}</div>
        <div class="muted" style="margin-top:2px;">${t('referrals.l2_desc')}</div>
      </div>
      <span class="reward outline">+${state.config.ref_l2}</span>
    </div>

    <h2>${t('referrals.stats_title')}</h2>
    <div class="grid-2">
      <div class="stat-tile">
        <div class="label">${t('referrals.stats_direct')}</div>
        <div class="value sm mono" id="stat-direct">—<span style="font-size:12px; color:var(--text-3); font-family:inherit; font-weight:500;"> ${t('referrals.stats_unit_person')}</span></div>
      </div>
      <div class="stat-tile">
        <div class="label">${t('referrals.stats_indirect')}</div>
        <div class="value sm mono" id="stat-indirect">—<span style="font-size:12px; color:var(--text-3); font-family:inherit; font-weight:500;"> ${t('referrals.stats_unit_person')}</span></div>
      </div>
    </div>
    <div class="stat-tile" style="margin-top:8px;">
      <div class="label">${t('referrals.stats_earned')}</div>
      <div class="value sm mono text-pos" id="stat-earned">—</div>
    </div>

    <h2>${t('referrals.tree_title')}</h2>
    <div class="card tight" id="tree-area">
      <div class="empty">${t('common.loading')}</div>
    </div>
  `;

  const stats = await api('/api/referrals/stats').catch(() => ({direct_count:0, indirect_count:0, tokens_earned:0, invite_url: location.origin + '?ref=' + state.user.referral_code}));
  document.getElementById('stat-direct').firstChild.textContent = stats.direct_count;
  document.getElementById('stat-indirect').firstChild.textContent = stats.indirect_count;
  document.getElementById('stat-earned').textContent = '+' + stats.tokens_earned;
  document.getElementById('btn-copy-link').onclick = () => copy(stats.invite_url);

  if (canEnterCode) {
    const inEl = document.getElementById('ref-code-in');
    document.getElementById('ref-code-apply').onclick = async () => {
      const code = (inEl.value || '').trim().toUpperCase();
      if (!code) return;
      try {
        const r = await api('/api/referrals/apply_code', {
          method: 'POST', body: JSON.stringify({ code }),
        });
        showToast('✓ ' + t('referrals.code_applied'));
        if (r.tokens) state.user.tokens = r.tokens;
        pageReferrals();
      } catch (e) {
        showToast(e.status === 404 || e.status === 400 ? t('referrals.code_invalid') : (e.body || e.message));
      }
    };
  }

  try {
    const tree = await api('/api/referrals/tree');
    const area = document.getElementById('tree-area');
    area.innerHTML = (tree.children?.length)
      ? renderRefTree(tree)
      : `<div class="empty">${t('referrals.no_referrals')}</div>`;
  } catch {}
}

// ─── Disclaimer / Safety ─────────────────────────────────────────
function disclaimerAccepted() { return localStorage.getItem('streak_disclaimer_v1') === 'yes'; }
function setDisclaimerAccepted(v) { localStorage.setItem('streak_disclaimer_v1', v ? 'yes' : ''); }

function openDisclaimerModal(onAccept) {
  const overlay = document.createElement('div');
  overlay.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1100;
    display:flex;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(6px);overflow:auto;`;
  overlay.innerHTML = `
    <div style="background:var(--bg);border-radius:20px;padding:24px;max-width:520px;width:100%;
                box-shadow:var(--shadow-lg);max-height:92vh;overflow:auto;">
      <div style="font-size:32px;text-align:center;">⚠️</div>
      <h2 style="margin:8px 0 4px;text-align:center;">${t('disclaimer.full_title')}</h2>
      <p style="text-align:center;color:var(--text-3);font-size:13px;margin:0 0 16px;">${t('disclaimer.short')}</p>
      <ol style="margin:0 0 16px;padding-left:20px;font-size:13px;line-height:1.65;color:var(--text-2);">
        <li style="margin-bottom:8px;">${t('disclaimer.p1')}</li>
        <li style="margin-bottom:8px;">${t('disclaimer.p2')}</li>
        <li style="margin-bottom:8px;">${t('disclaimer.p3')}</li>
        <li style="margin-bottom:8px;">${t('disclaimer.p4')}</li>
        <li style="margin-bottom:8px;">${t('disclaimer.p5')}</li>
        <li style="margin-bottom:8px;">${t('disclaimer.p6')}</li>
      </ol>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;
                    background:var(--bg-2);padding:12px;border-radius:10px;margin-bottom:12px;">
        <input type="checkbox" id="dc-chk" />
        <span style="font-size:13px;">${t('disclaimer.agree')}</span>
      </label>
      <div class="row" style="gap:8px;">
        <button class="btn" id="dc-ok" disabled style="flex:1;opacity:.5;">${t('common.save') || 'OK'}</button>
        <button class="btn ghost" id="dc-cancel">${t('common.cancel')}</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.onclick = (e)=>{ if (e.target===overlay) close(); };
  const chk = overlay.querySelector('#dc-chk');
  const ok = overlay.querySelector('#dc-ok');
  chk.onchange = () => { ok.disabled = !chk.checked; ok.style.opacity = chk.checked ? 1 : .5; };
  ok.onclick = () => {
    if (!chk.checked) return;
    setDisclaimerAccepted(true);
    close();
    if (typeof onAccept === 'function') onAccept();
  };
  overlay.querySelector('#dc-cancel').onclick = close;
}

// ─── LIVE strategy engine (browser-side) ─────────────────────────
// PK 는 sessionStorage 또는 localStorage. 서버 전송은 매 거래 시점에만.
const LIVE = {
  loopId: null,
  status: 'stopped',
  walletInfo: null,
  trades: [],     // local-only trade history
  positions: [],  // local-only open positions
  market: null,
};

function pkGet() {
  return sessionStorage.getItem('streak_pk') || localStorage.getItem('streak_pk') || '';
}
function pkSet(pk, persist) {
  pkClear();
  if (persist) localStorage.setItem('streak_pk', pk);
  else sessionStorage.setItem('streak_pk', pk);
}
function pkClear() {
  sessionStorage.removeItem('streak_pk');
  localStorage.removeItem('streak_pk');
  sessionStorage.removeItem('streak_funder');
  localStorage.removeItem('streak_funder');
}
function funderGet() {
  return sessionStorage.getItem('streak_funder') || localStorage.getItem('streak_funder') || '';
}

function tradesLoad() {
  try { LIVE.trades = JSON.parse(localStorage.getItem('streak_trades') || '[]'); }
  catch { LIVE.trades = []; }
  try { LIVE.positions = JSON.parse(localStorage.getItem('streak_positions') || '[]'); }
  catch { LIVE.positions = []; }
}
function tradesSave() {
  localStorage.setItem('streak_trades', JSON.stringify(LIVE.trades.slice(-200)));
  localStorage.setItem('streak_positions', JSON.stringify(LIVE.positions));
}

function shouldEnterJS(cfg, mkt) {
  const elapsedPct = mkt.elapsed_pct;
  if (elapsedPct >= cfg.tradeable_pct) return null;
  const remaining = 1 - elapsedPct;
  if (remaining > cfg.buy_when_remaining_below_pct) return null;

  const yb = mkt.yes_book.best_bid, ya = mkt.yes_book.best_ask;
  const nb = mkt.no_book.best_bid,  na = mkt.no_book.best_ask;

  if (cfg.entry_mode === 'low_target') {
    const lo = cfg.entry_price - cfg.entry_tolerance;
    const hi = cfg.entry_price + cfg.entry_tolerance;
    const cands = [];
    if (lo <= ya && ya <= hi) cands.push({side:'YES', token:mkt.market.yes_token, ask:ya});
    if (lo <= na && na <= hi) cands.push({side:'NO',  token:mkt.market.no_token,  ask:na});
    if (!cands.length) return null;
    cands.sort((a,b)=>Math.abs(a.ask-cfg.entry_price)-Math.abs(b.ask-cfg.entry_price));
    const c = cands[0];
    return { side: c.side, token_id: c.token,
             price: cfg.buy_order_type==='limit' ? cfg.entry_price : c.ask };
  }
  // high_lead
  const leading = yb > nb ? {side:'YES', token:mkt.market.yes_token, ask:ya, bid:yb}
                          : {side:'NO',  token:mkt.market.no_token,  ask:na, bid:nb};
  if (leading.ask >= cfg.entry_price && leading.ask <= cfg.max_entry_price) {
    return { side: leading.side, token_id: leading.token,
             price: cfg.buy_order_type==='limit' ? cfg.entry_price : leading.ask };
  }
  return null;
}

function shouldExitJS(pos, cfg, mkt) {
  const isYes = pos.side === 'YES';
  const bid = isYes ? mkt.yes_book.best_bid : mkt.no_book.best_bid;
  if (bid >= cfg.tp_price) return { price: cfg.sell_order_type==='limit'?cfg.tp_price:bid, reason:'tp' };
  if (bid <= cfg.sl_price) return { price: cfg.sell_order_type==='limit'?cfg.sl_price:bid, reason:'sl' };
  return null;
}

async function liveTick(cfg) {
  if (LIVE._tickInflight) return;       // 중복 틱 방지
  LIVE._tickInflight = true;
  try {
    const pk = pkGet();
    if (!pk) { liveStop(); return; }
    // 1) 마켓 데이터
    let mkt;
    try { mkt = await api(`/api/trading/market_data?asset=${cfg.asset}&duration_min=${cfg.duration_min}`); }
    catch { return; }
    if (!mkt.available) return;
    LIVE.market = mkt;

    // 2) 청산 체크 (현재 마켓에 우리 포지션 있으면)
    for (const pos of [...LIVE.positions]) {
      if (pos.market_slug !== mkt.market.slug) continue;
      if (pos._sellInflight) continue;
      const ex = shouldExitJS(pos, cfg, mkt);
      if (!ex) continue;
      pos._sellInflight = true;
      const idem = `sell-${pos.id}-${Math.round(ex.price*100)}-${ex.reason}`;
      try {
        const r = await api('/api/trading/execute', {
          method: 'POST',
          body: JSON.stringify({
            private_key: pk, funder: funderGet() || null,
            action: 'sell', token_id: pos.token_id,
            price: ex.price, size: pos.size,
            order_type: cfg.sell_order_type,
            market_slug: mkt.market.slug,
            idempotency_key: idem,
            tick_size: mkt.market.tick_size || 0.01,
            neg_risk: !!mkt.market.neg_risk,
          }),
        });
        if (r.ok) {
          pos.exit_price = ex.price;
          pos.exit_reason = ex.reason;
          pos.closed_at = new Date().toISOString();
          pos.pnl = (ex.price - pos.entry_price) * pos.size;
          LIVE.trades.push({ ...pos, kind:'sell', ok:true });
          LIVE.positions = LIVE.positions.filter(p => p.id !== pos.id);
        } else {
          // 매도 실패 → 포지션 유지, 다음 틱 재시도
          LIVE.trades.push({ kind:'sell', ok:false,
            error: r.error, error_code: r.error_code,
            market_slug: mkt.market.slug, side: pos.side,
            opened_at: new Date().toISOString() });
          delete pos._sellInflight;
        }
        tradesSave();
      } catch (e) {
        console.error('sell err', e);
        delete pos._sellInflight;
      }
    }

    // 3) 진입 체크 (이미 이 마켓에 포지션 없을 때만)
    const hasInMkt = LIVE.positions.some(p => p.market_slug === mkt.market.slug);
    if (!hasInMkt && !LIVE._buyInflight) {
      const dec = shouldEnterJS(cfg, mkt);
      if (dec) {
        const ya = mkt.yes_book.best_ask, na = mkt.no_book.best_ask;
        const ask = dec.side === 'YES' ? ya : na;
        if (cfg.buy_order_type === 'limit' && ask > dec.price + 0.001) {
          // 호가 미도달 — 다음 틱
        } else {
          const fillPx = cfg.buy_order_type === 'market' ? ask : dec.price;
          const sz = Math.max(5.0, Math.round(cfg.bet_size_usd / Math.max(0.01, fillPx) * 100) / 100);
          const idem = `buy-${mkt.market.slug}-${dec.side}-${Math.round(fillPx*100)}`;
          LIVE._buyInflight = true;
          try {
            const r = await api('/api/trading/execute', {
              method: 'POST',
              body: JSON.stringify({
                private_key: pk, funder: funderGet() || null,
                action: 'buy', token_id: dec.token_id,
                price: fillPx, size: sz,
                order_type: cfg.buy_order_type,
                market_slug: mkt.market.slug,
                max_price: cfg.buy_order_type === 'market' ? Math.min(0.99, fillPx + 0.02) : null,
                idempotency_key: idem,
                tick_size: mkt.market.tick_size || 0.01,
                neg_risk: !!mkt.market.neg_risk,
              }),
            });
            if (r.ok && !r.idempotent_replay) {
              const pos = {
                id: 'p_' + Date.now(),
                market_slug: mkt.market.slug,
                market_label: `${cfg.asset} ${cfg.duration_min}m ${dec.side}`,
                side: dec.side, token_id: dec.token_id,
                entry_price: fillPx, size: sz,
                opened_at: new Date().toISOString(),
                tokens_left: r.tokens_left, address: r.address,
              };
              LIVE.positions.push(pos);
              LIVE.trades.push({ ...pos, kind:'buy', ok:true });
              tradesSave();
              if (state.user) state.user.tokens = r.tokens_left;
            } else if (!r.ok) {
              LIVE.trades.push({ kind:'buy', ok:false,
                error: r.error, error_code: r.error_code,
                market_slug: mkt.market.slug, side: dec.side,
                opened_at: new Date().toISOString() });
              tradesSave();
              if (state.user) state.user.tokens = r.tokens_left;
              // 치명적 에러는 봇 자동 정지
              if (['insufficient_balance','allowance_required','invalid_pk','signature_error']
                    .includes(r.error_code)) {
                liveStop();
                showToast(r.error_code);
              }
            }
            if (state.page === 'trading') pageTrading();
          } catch (e) {
            console.error('buy err', e);
            if (e.status === 402) { liveStop(); showToast(t('trading.need_tokens')); }
          } finally {
            LIVE._buyInflight = false;
          }
        }
      }
    }
  } finally {
    LIVE._tickInflight = false;
  }
}

function liveStart(cfg) {
  if (LIVE.loopId) return;
  LIVE.status = 'running';
  liveTick(cfg);
  LIVE.loopId = setInterval(() => liveTick(cfg), 5000);
}
function liveStop() {
  if (LIVE.loopId) clearInterval(LIVE.loopId);
  LIVE.loopId = null;
  LIVE.status = 'stopped';
}

function modeGet() { return localStorage.getItem('streak_mode') || 'paper'; }
function modeSet(m) { localStorage.setItem('streak_mode', m); }

// PK 모달
function openPkModal() {
  const overlay = document.createElement('div');
  overlay.style.cssText = `position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;
    display:flex;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(6px);overflow:auto;`;
  overlay.innerHTML = `
    <div style="background:var(--bg);border-radius:20px;padding:24px;max-width:440px;width:100%;
                box-shadow:var(--shadow-lg);max-height:92vh;overflow:auto;">
      <h2 style="margin:0 0 8px;">${t('trading.pk_form_title')}</h2>
      <div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);
                  border-radius:12px;padding:12px;margin-bottom:14px;">
        <ul style="margin:0;padding-left:18px;font-size:12px;line-height:1.6;color:var(--text-2);">
          <li>${t('trading.pk_form_warn1')}</li>
          <li>${t('trading.pk_form_warn2')}</li>
          <li>${t('trading.pk_form_warn3')}</li>
        </ul>
      </div>
      <div style="margin-bottom:10px;">
        <label style="font-size:12px;color:var(--text-3);">${t('trading.pk_input')}</label>
        <input id="pk-in" type="password" autocomplete="off" placeholder="0x..."
               style="width:100%;margin-top:4px;padding:10px;border-radius:10px;
                      border:1px solid var(--border);background:var(--bg-2);color:var(--text);
                      font-family:monospace;font-size:12px;" />
      </div>
      <div style="margin-bottom:10px;">
        <label style="font-size:12px;color:var(--text-3);">${t('trading.pk_funder')}</label>
        <input id="pk-fd" type="text" autocomplete="off" placeholder="0x..." value="${funderGet()}"
               style="width:100%;margin-top:4px;padding:10px;border-radius:10px;
                      border:1px solid var(--border);background:var(--bg-2);color:var(--text);
                      font-family:monospace;font-size:12px;" />
      </div>
      <div style="margin-bottom:14px;">
        <label style="font-size:12px;color:var(--text-3);">${t('trading.pk_storage')}</label>
        <div class="row" style="gap:6px;margin-top:6px;">
          <label style="font-size:12px;flex:1;cursor:pointer;">
            <input type="radio" name="pk-st" value="session" checked /> ${t('trading.pk_session')}</label>
          <label style="font-size:12px;flex:1;cursor:pointer;">
            <input type="radio" name="pk-st" value="local" /> ${t('trading.pk_local')}</label>
        </div>
      </div>
      <div class="row" style="gap:8px;">
        <button class="btn" id="pk-go" style="flex:1;">${t('trading.pk_check')}</button>
        <button class="btn ghost" id="pk-cancel">${t('common.cancel')}</button>
      </div>
      <button class="btn danger sm" id="pk-clr" style="width:100%;margin-top:10px;">
        ${t('trading.pk_clear')}
      </button>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.onclick = (e)=>{ if (e.target===overlay) close(); };
  overlay.querySelector('#pk-cancel').onclick = close;
  overlay.querySelector('#pk-clr').onclick = ()=>{
    pkClear(); LIVE.walletInfo=null; liveStop();
    showToast(t('trading.pk_cleared')); close();
    if (state.page==='trading') pageTrading();
  };
  overlay.querySelector('#pk-go').onclick = async ()=>{
    let pk = overlay.querySelector('#pk-in').value.trim();
    const fd = overlay.querySelector('#pk-fd').value.trim();
    const persist = overlay.querySelector('input[name=pk-st]:checked').value === 'local';
    if (pk.replace(/^0x/, '').length !== 64) { showToast(t('trading.live_invalid_pk')); return; }
    try {
      const info = await api('/api/trading/wallet_check', {
        method:'POST',
        body: JSON.stringify({ private_key: pk, funder: fd || null }),
      });
      if (info.error) { showToast(info.error); return; }
      pkSet(pk, persist);
      if (fd) (persist?localStorage:sessionStorage).setItem('streak_funder', fd);
      LIVE.walletInfo = info;
      pk = null;     // GC hint
      showToast('✓');
      close();
      if (state.page==='trading') pageTrading();
    } catch (e) { showToast(e.body || e.message); }
    finally { pk = null; }
  };
}

async function pageTrading() {
  const main = document.getElementById('main');
  main.innerHTML = `<div class="card"><div class="empty">${t('common.loading')}</div></div>`;

  // 각 endpoint 독립 fallback (서버 미배포여도 페이지가 죽지 않게)
  const safeApi = async (path, fallback) => {
    try { return await api(path); }
    catch (e) { console.warn('api fail', path, e.status, e.body); return fallback; }
  };
  const [cfg, stats, openPos, history, assets] = await Promise.all([
    safeApi('/api/trading/config', {
      active:false, asset:'BTC', duration_min:5, entry_mode:'low_target',
      bet_size_usd:1, entry_price:0.10, entry_tolerance:0.01, max_entry_price:0.85,
      tp_price:0.15, sl_price:0.05, buy_order_type:'limit', sell_order_type:'limit',
      tradeable_pct:0.60, buy_when_remaining_below_pct:1.0,
      max_cycles_per_session:0, cycles_consumed:0,
    }),
    safeApi('/api/trading/stats', {
      total_trades:0, wins:0, losses:0, win_rate:0, total_pnl:0,
      open_count:0, market_state:{},
    }),
    safeApi('/api/trading/positions', []),
    safeApi('/api/trading/history?limit=20', []),
    safeApi('/api/trading/assets', [
      {code:'BTC',label:'Bitcoin',icon:'₿',active_durations:[5],coming_soon_durations:[15,60]},
      {code:'ETH',label:'Ethereum',icon:'Ξ',active_durations:[],coming_soon_durations:[5,15,60]},
      {code:'SOL',label:'Solana',icon:'◎',active_durations:[],coming_soon_durations:[5,15,60]},
    ]),
  ]);

  const selectedStrategy = cfg.entry_mode || cfg.strategy || 'low_target';
  tradesLoad();
  const mode = modeGet();
  const isLive = mode === 'live';
  const havePk = !!pkGet();

  // 라이브 모드면 LIVE.market 사용 (브라우저-주도 폴링)
  let ms, closeTs, elapsedPct;
  if (isLive && LIVE.market && LIVE.market.available) {
    const lm = LIVE.market;
    ms = { slug: lm.market.slug, question: lm.market.question,
           end_ts: lm.market.end_ts, elapsed_pct: lm.elapsed_pct };
    closeTs = new Date(lm.market.end_ts * 1000);
    elapsedPct = lm.elapsed_pct * 100;
  } else {
    ms = stats.market_state || {};
    closeTs = ms.end_ts ? new Date(ms.end_ts * 1000) : null;
    elapsedPct = (ms.elapsed_pct || 0) * 100;
  }
  const sideBadge = (s) => s === 'YES'
    ? `<span style="background:rgba(34,197,94,.15); color:#22c55e; padding:2px 8px; border-radius:8px; font-size:11px; font-weight:600;">YES</span>`
    : `<span style="background:rgba(239,68,68,.15); color:#ef4444; padding:2px 8px; border-radius:8px; font-size:11px; font-weight:600;">NO</span>`;
  const pnlPill = (v) => {
    const cls = v >= 0 ? 'pos' : 'neg';
    const sign = v >= 0 ? '+' : '';
    const color = v >= 0 ? '#22c55e' : '#ef4444';
    return `<span style="color:${color}; font-weight:700;">${sign}$${Math.abs(v).toFixed(2)}</span>`;
  };

  main.innerHTML = `
    <h1>${t('trading.title')}</h1>

    <section class="card hero">
      <div class="row between">
        <div>
          <div class="label">${t('trading.bot_status')}</div>
          <div class="value sm" style="margin-top:4px;">
            ${(isLive ? LIVE.status === 'running' : cfg.active)
              ? `<span style="color:#22c55e;">● ${isLive ? t('trading.live_loop_running') : t('trading.running')}</span>`
              : `<span style="color:var(--text-3);">● ${isLive ? t('trading.live_loop_stopped') : t('trading.stopped')}</span>`}
          </div>
          ${ms.slug ? `
            <div class="sub" style="margin-top:8px; font-size:12px;">
              ${t('trading.current_market')}: <strong>${ms.question || ms.slug}</strong>
              ${closeTs ? ` · <span id="td-cd">${t('trading.next_cycle')}: <span id="td-cd-num">…</span></span>` : ''}
            </div>
            <div style="margin-top:8px; height:6px; background:var(--bg-2); border-radius:3px; overflow:hidden;">
              <div style="height:100%; width:${elapsedPct.toFixed(0)}%; background:var(--primary); transition:width 1s;"></div>
            </div>
            <div class="sub" style="margin-top:4px; font-size:11px; color:var(--text-3);">
              ${elapsedPct.toFixed(0)}% ${t('trading.elapsed')} · ${(100-elapsedPct).toFixed(0)}% ${t('trading.remaining')}
            </div>
            ` : ''}
        </div>
        <div>
          ${(isLive ? LIVE.status === 'running' : cfg.active)
            ? `<button class="btn sm danger" id="btn-trade-stop">${t('trading.stop_btn')}</button>`
            : `<button class="btn sm" id="btn-trade-start">${t('trading.start_btn')} →</button>`}
        </div>
      </div>
      <p style="margin:12px 0 0; font-size:12px; color:var(--text-2);">${t('trading.auto_desc')}</p>
    </section>

    <section class="safety-strip" style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;">
      <div class="chip green">🔒 ${t('safety.badge')}</div>
      <div class="chip">⚠ ${t('disclaimer.short')}</div>
      <button class="chip link" id="btn-show-disclaimer">${t('disclaimer.title')} →</button>
    </section>

    <section class="card" style="margin-top:12px;">
      <div class="row between">
        <div>
          <div class="label">Mode</div>
          <div class="row" style="gap:6px;margin-top:6px;">
            <button class="btn sm ${!isLive?'':'ghost'}" data-mode="paper">${t('trading.paper_mode')}</button>
            <button class="btn sm ${isLive?'':'ghost'}" data-mode="live">${t('trading.live_mode')}</button>
          </div>
        </div>
        ${isLive ? `
          <div style="text-align:right;">
            <div class="label">${t('trading.wallet_section') || 'Wallet'}</div>
            ${havePk && LIVE.walletInfo ? `
              <div style="font-family:monospace;font-size:12px;margin-top:4px;">${LIVE.walletInfo.address?.slice(0,6)}…${LIVE.walletInfo.address?.slice(-4)}</div>
              <div class="sub" style="font-size:11px;">$${(LIVE.walletInfo.balance_usdc||0).toFixed(2)} · all $${(LIVE.walletInfo.allowance_usdc||0).toFixed(2)}</div>
            ` : `<div class="sub" style="color:var(--neg, #ef4444);">${t('trading.live_no_pk')}</div>`}
            <button class="btn sm ghost" id="btn-pk" style="margin-top:6px;">
              ${havePk ? '✎' : t('trading.wallet_connect')}
            </button>
          </div>` : ''}
      </div>
      ${isLive ? `<p class="sub" style="font-size:11px;margin:8px 0 0;color:var(--text-3);">${t('trading.tab_warning')}</p>` : ''}
    </section>

    <section class="card" style="margin-top:12px;">
      <div class="label">${t('trading.asset_selector')}</div>
      <div class="row" style="gap:8px; margin-top:8px; flex-wrap:wrap;">
        ${assets.map(a => {
          const allDurs = [...a.active_durations, ...a.coming_soon_durations].sort((x,y)=>x-y);
          return allDurs.map(d => {
            const enabled = a.active_durations.includes(d);
            const sel = (cfg.asset === a.code && cfg.duration_min === d);
            return `<button class="btn sm ${sel?'':'ghost'}" data-asset="${a.code}" data-dur="${d}"
                            ${enabled?'':'disabled style="opacity:.4; cursor:not-allowed;"'}>
              ${a.icon} ${a.code} ${d}m${enabled?'':' · '+t('trading.coming_soon')}
            </button>`;
          }).join('');
        }).join('')}
      </div>
    </section>

    <section class="grid-2" style="margin-top:12px;">
      <div class="card"><div class="label">${t('trading.stats_total')}</div><div class="value sm">${stats.total_trades}</div></div>
      <div class="card"><div class="label">${t('trading.stats_winrate')}</div><div class="value sm">${(stats.win_rate*100).toFixed(0)}%</div></div>
      <div class="card"><div class="label">${t('trading.stats_pnl')}</div><div class="value sm">${pnlPill(stats.total_pnl)}</div></div>
      <div class="card"><div class="label">${t('trading.stats_consumed')}</div><div class="value sm">${cfg.cycles_consumed}</div></div>
    </section>

    ${(state.user.tokens < (state.config.cost_per_cycle || 1)) ? `
      <section class="card" style="border-left:3px solid var(--neg, #ef4444); margin-top:12px;">
        <p style="margin:0;">⚠ ${t('trading.need_tokens')}</p>
        <button class="btn ghost sm" id="btn-need-go" style="margin-top:8px;">→ ${t('referrals.title')}</button>
      </section>
    ` : ''}

    <section style="margin-top:24px;">
      <h2>${t('trading.strategy_presets')}</h2>
      <div class="row" style="gap:8px; margin-bottom:8px; flex-wrap:wrap;">
        <button class="btn sm ${selectedStrategy==='low_target'?'':'ghost'}" data-preset="low_target">🎯 ${t('trading.preset_low')}</button>
        <button class="btn sm ${selectedStrategy==='high_lead'?'':'ghost'}" data-preset="high_lead">📈 ${t('trading.preset_lead')}</button>
      </div>
      <details ${cfg.active?'':'open'}>
        <summary style="cursor:pointer; font-weight:600; padding:8px 0;">⚙ ${t('trading.advanced')}</summary>
        <p style="font-size:12px; color:var(--text-3); margin:4px 0 12px;">${t('trading.advanced_hint')}</p>
        <div class="card">
          <div class="grid-2" style="gap:10px;">
            <label class="form-row"><span>${t('trading.p_bet_size')}</span>
              <input type="number" id="f-bet" min="0.5" max="1000" step="0.5" value="${cfg.bet_size_usd}" /></label>
            <label class="form-row"><span>${t('trading.p_max_cycles')}</span>
              <input type="number" id="f-max" min="0" max="10000" step="1" value="${cfg.max_cycles_per_session}" /></label>
            <label class="form-row"><span>${t('trading.p_entry_price')}</span>
              <input type="number" id="f-entry" min="0.01" max="0.99" step="0.01" value="${cfg.entry_price}" /></label>
            <label class="form-row"><span>${t('trading.p_entry_tol')}</span>
              <input type="number" id="f-tol" min="0" max="0.5" step="0.01" value="${cfg.entry_tolerance}" /></label>
            <label class="form-row"><span>${t('trading.p_max_entry')}</span>
              <input type="number" id="f-maxe" min="0.5" max="0.99" step="0.01" value="${cfg.max_entry_price}" /></label>
            <label class="form-row"><span>${t('trading.p_tp')}</span>
              <input type="number" id="f-tp" min="0.02" max="0.99" step="0.01" value="${cfg.tp_price}" /></label>
            <label class="form-row"><span>${t('trading.p_sl')}</span>
              <input type="number" id="f-sl" min="0.01" max="0.5" step="0.01" value="${cfg.sl_price}" /></label>
            <label class="form-row"><span>${t('trading.p_tradeable')}</span>
              <input type="number" id="f-trd" min="0.05" max="1" step="0.05" value="${cfg.tradeable_pct}" /></label>
            <label class="form-row"><span>${t('trading.p_remaining')}</span>
              <input type="number" id="f-rem" min="0.05" max="1" step="0.05" value="${cfg.buy_when_remaining_below_pct}" /></label>
            <label class="form-row"><span>${t('trading.p_buy_type')}</span>
              <select id="f-buyt">
                <option value="limit" ${cfg.buy_order_type==='limit'?'selected':''}>limit</option>
                <option value="market" ${cfg.buy_order_type==='market'?'selected':''}>market</option>
              </select></label>
            <label class="form-row"><span>${t('trading.p_sell_type')}</span>
              <select id="f-sellt">
                <option value="limit" ${cfg.sell_order_type==='limit'?'selected':''}>limit</option>
                <option value="market" ${cfg.sell_order_type==='market'?'selected':''}>market</option>
              </select></label>
          </div>
          <button class="btn" id="btn-strat-save" style="width:100%; margin-top:12px;">${t('trading.p_save')}</button>
          <p style="font-size:11px; color:var(--text-3); margin:8px 0 0;">${t('trading.token_used_msg')}</p>
        </div>
      </details>
    </section>

    <section style="margin-top:24px;">
      <h2>${t('trading.open_positions')}</h2>
      ${(() => {
        const list = isLive ? LIVE.positions : openPos;
        if (!list.length) return `<div class="card"><div class="empty">${t('trading.no_open')}</div></div>`;
        return list.map(p => `
          <div class="card">
            <div class="row between">
              <div>
                <div style="font-size:13px; color:var(--text-2);">${p.market_label || p.market_slug || ''}</div>
                <div style="margin-top:4px;">${sideBadge(p.side)} <span style="font-size:12px; color:var(--text-3);">@ $${(p.entry_price||0).toFixed(2)} · ${(p.size||0).toFixed(2)} sh</span></div>
              </div>
              <div style="font-size:11px; color:var(--text-3); text-align:right;">${p.strategy || ''}</div>
            </div>
          </div>`).join('');
      })()}
    </section>

    <section style="margin-top:24px;">
      <h2>${isLive ? t('trading.trades_local') : t('trading.history_title')}</h2>
      ${(() => {
        const list = isLive
          ? LIVE.trades.slice().reverse().slice(0, 30)
          : history;
        const emptyMsg = isLive ? t('trading.no_trades_local') : t('trading.no_history');
        if (!list.length) return `<div class="card"><div class="empty">${emptyMsg}</div></div>`;
        if (isLive) {
          return `<div class="tx-list">${list.map(p => {
            const ts = new Date(p.closed_at || p.opened_at).toLocaleString();
            const pnl = (typeof p.pnl === 'number') ? p.pnl : null;
            const pnlStr = pnl != null ? `${pnl>=0?'+':''}$${pnl.toFixed(2)}` : '';
            const okIcon = p.ok ? '✓' : '✗';
            return `<div class="tx-item">
              <div>
                <div>${sideBadge(p.side||'YES')} <span style="font-size:12px;">${p.kind||''} ${p.market_slug||''}</span></div>
                <div class="meta">${ts} · ${okIcon} ${p.error || p.exit_reason || ''}</div>
              </div>
              <div class="tx-amount ${pnl>=0?'pos':'neg'}">${pnlStr}</div>
            </div>`;
          }).join('')}</div>`;
        }
        return `<div class="tx-list">${list.map(p => `
            <div class="tx-item">
              <div>
                <div>${sideBadge(p.side)} <span style="font-size:12px;">${p.market_label}</span></div>
                <div class="meta">${new Date(p.closed_at || p.opened_at).toLocaleString()} · ${(p.exit_reason||'').startsWith('tp')||p.exit_reason==='expiry_win' ? '✓ '+t('trading.win') : '✗ '+t('trading.loss')}</div>
              </div>
              <div class="tx-amount ${p.pnl>=0?'pos':'neg'}">${p.pnl>=0?'+':''}$${(p.pnl||0).toFixed(2)}</div>
            </div>`).join('')}</div>`;
      })()}
    </section>

    <footer class="site-footer" style="margin:24px 0 8px; text-align:center; font-size:11px; color:var(--text-3); line-height:1.6;">
      ${t('disclaimer.footer_short')}
    </footer>
  `;

  // ─── handlers ────────────────────────────────────────
  const startBtn = document.getElementById('btn-trade-start');
  const stopBtn = document.getElementById('btn-trade-stop');
  if (startBtn) startBtn.onclick = async () => {
    if (isLive) {
      if (!disclaimerAccepted()) {
        openDisclaimerModal(() => startBtn.click());
        return;
      }
      if (!havePk) { openPkModal(); return; }
      liveStart(cfg);
      showToast('✓ ' + t('trading.live_loop_running'));
      pageTrading();
      return;
    }
    try {
      await api('/api/trading/start', { method: 'POST' });
      showToast('✓ ' + t('trading.running'));
      pageTrading();
    } catch (e) {
      if (e.status === 402) showToast(t('trading.need_tokens'));
      else showToast(e.body || e.message || t('common.error'));
    }
  };
  if (stopBtn && isLive) stopBtn.onclick = () => {
    liveStop(); showToast(t('trading.live_loop_stopped')); pageTrading();
  };

  const showDisc = document.getElementById('btn-show-disclaimer');
  if (showDisc) showDisc.onclick = () => openDisclaimerModal();

  // 모드 토글
  main.querySelectorAll('[data-mode]').forEach(b => {
    b.onclick = () => {
      modeSet(b.dataset.mode);
      liveStop();
      pageTrading();
    };
  });
  // PK 버튼
  const pkBtn = document.getElementById('btn-pk');
  if (pkBtn) pkBtn.onclick = () => openPkModal();

  // 자산 + duration 변경
  main.querySelectorAll('[data-asset]').forEach(b => {
    if (b.disabled) return;
    b.onclick = async () => {
      try {
        await api('/api/trading/config', {
          method: 'PUT',
          body: JSON.stringify({ asset: b.dataset.asset, duration_min: parseInt(b.dataset.dur) }),
        });
        pageTrading();
      } catch (e) { showToast(e.body || e.message); }
    };
  });

  // 프리셋 (entry_mode 빠른 변경)
  main.querySelectorAll('[data-preset]').forEach(b => {
    b.onclick = async () => {
      const preset = b.dataset.preset;
      const presets = {
        low_target: { entry_mode: 'low_target', entry_price: 0.10, tp_price: 0.15, sl_price: 0.05 },
        high_lead:  { entry_mode: 'high_lead',  entry_price: 0.70, tp_price: 0.95, sl_price: 0.50, max_entry_price: 0.85 },
      };
      try {
        await api('/api/trading/config', {
          method: 'PUT',
          body: JSON.stringify(presets[preset]),
        });
        pageTrading();
      } catch (e) { showToast(e.body || e.message); }
    };
  });

  // 고급 전략 저장
  const stratSave = document.getElementById('btn-strat-save');
  if (stratSave) stratSave.onclick = async () => {
    const body = {
      bet_size_usd: parseFloat(document.getElementById('f-bet').value),
      max_cycles_per_session: parseInt(document.getElementById('f-max').value),
      entry_price: parseFloat(document.getElementById('f-entry').value),
      entry_tolerance: parseFloat(document.getElementById('f-tol').value),
      max_entry_price: parseFloat(document.getElementById('f-maxe').value),
      tp_price: parseFloat(document.getElementById('f-tp').value),
      sl_price: parseFloat(document.getElementById('f-sl').value),
      tradeable_pct: parseFloat(document.getElementById('f-trd').value),
      buy_when_remaining_below_pct: parseFloat(document.getElementById('f-rem').value),
      buy_order_type: document.getElementById('f-buyt').value,
      sell_order_type: document.getElementById('f-sellt').value,
    };
    try {
      await api('/api/trading/config', { method: 'PUT', body: JSON.stringify(body) });
      showToast('✓ ' + t('trading.p_saved'));
      pageTrading();
    } catch (e) { showToast(e.body || e.message); }
  };
  if (stopBtn) stopBtn.onclick = async () => {
    try {
      await api('/api/trading/stop', { method: 'POST' });
      showToast(t('trading.stopped'));
      pageTrading();
    } catch (e) { showToast(e.message); }
  };
  const needGo = document.getElementById('btn-need-go');
  if (needGo) needGo.onclick = () => navigate('referrals');

  // 라이브 + PK 있는데 walletInfo 미로드 시 한 번 조회
  if (isLive && havePk && !LIVE.walletInfo) {
    api('/api/trading/wallet_check', {
      method:'POST',
      body: JSON.stringify({ private_key: pkGet(), funder: funderGet() || null }),
    }).then(info => {
      LIVE.walletInfo = info;
      if (state.page==='trading') pageTrading();
    }).catch(()=>{});
  }

  // countdown
  if (closeTs) {
    const cd = document.getElementById('td-cd-num');
    const tick = () => {
      if (!document.body.contains(cd)) return;
      const left = Math.max(0, Math.floor((closeTs - new Date()) / 1000));
      const m = Math.floor(left / 60).toString().padStart(2,'0');
      const s = (left % 60).toString().padStart(2,'0');
      cd.textContent = `${m}:${s}`;
      if (left > 0) setTimeout(tick, 1000);
      else setTimeout(() => { if (state.page === 'trading') pageTrading(); }, 5000);
    };
    tick();
  }
}

function pageSettings() {
  const main = document.getElementById('main');
  const isAnon = state.user.auth_method === 'anonymous';
  const initial = (state.user.email || 'U').slice(0, 1).toUpperCase();
  const joined = state.user.created_at ? new Date(state.user.created_at).toLocaleDateString(state.lang==='ko'?'ko-KR':'en-US') : '';
  const provider = state.user.auth_method === 'google' || state.user.auth_method === 'upgraded' ? 'Google' : 'Guest';

  main.innerHTML = `
    <h1>${t('settings.title')}</h1>

    <div class="card tight" style="display:flex; align-items:center; gap:12px;">
      <div style="width:42px; height:42px; border-radius:10px; background:var(--text); color:#fff; display:flex; align-items:center; justify-content:center; font-size:14px; font-weight:700; flex-shrink:0; letter-spacing:0.04em;">${initial}</div>
      <div style="flex:1;">
        <div style="font-weight:600; font-size:14px;">${state.user.email || shortAddr(state.user.address || '')}</div>
        <div class="muted" style="margin-top:2px;">${provider}${joined ? ' · ' + joined : ''}</div>
      </div>
    </div>

    ${isAnon ? `
      <div class="nudge" style="margin-top:8px;">
        <h3 style="margin:0;">${t('home.upgrade_title')}</h3>
        <p style="margin-top:4px;">${t('home.upgrade_desc', { tokens: state.config.upgrade_bonus })}</p>
        <button class="btn" id="btn-upgrade-set" style="margin-top:12px; padding:10px 14px; font-size:13px;">${t('home.upgrade_cta')}</button>
      </div>
    ` : ''}

    <h2>${t('settings.general')}</h2>
    <div class="card tight" style="padding:0;">
      <div style="padding:14px 16px; border-bottom:1px solid var(--border);">
        <div class="row between">
          <div style="font-weight:500; font-size:14px;">${t('settings.language')}</div>
          <span class="muted">${{en:'English', ko:'한국어', zh:'中文'}[state.lang]}</span>
        </div>
        <div class="row" style="margin-top:10px; gap:6px;">
          ${[['en','English'],['ko','한국어'],['zh','中文']].map(([k,v]) =>
            `<button class="btn sm ${state.lang===k?'':'outline'}" data-lang="${k}">${v}</button>`).join('')}
        </div>
      </div>
      <div style="padding:14px 16px;" class="row between">
        <div>
          <div style="font-weight:500; font-size:14px;">${t('safety.badge')}</div>
          <div class="muted" style="margin-top:2px;">${t('safety.subtitle')}</div>
        </div>
        <a href="https://github.com/yujoohwan6342-stack/POLY" target="_blank" rel="noopener" style="font-size:13px; font-weight:500; color:var(--text-2);">GitHub →</a>
      </div>
    </div>

    <h2>${t('settings.support')}</h2>
    <div class="card tight" style="padding:0;">
      <div class="set-row" id="set-disc">
        <span style="font-size:14px; font-weight:500; flex:1;">${t('disclaimer.title')}</span>
        <span style="color:var(--text-3);">›</span>
      </div>
      <a href="https://github.com/yujoohwan6342-stack/POLY/blob/main/README.md" target="_blank" rel="noopener" class="set-row">
        <span style="font-size:14px; font-weight:500; flex:1;">${t('settings.support_guide')}</span>
        <span style="color:var(--text-3);">›</span>
      </a>
    </div>

    <h2>${t('settings.account_section')}</h2>
    <button class="btn outline" id="btn-logout">${t('settings.logout')}</button>

    <p class="muted" style="text-align:center; margin-top:20px; font-size:11px;">${t('settings.version')}</p>
    <p class="muted" style="text-align:center; margin:4px 0 16px; font-size:11px; line-height:1.5;">
      ${t('disclaimer.footer_short')}
    </p>
  `;
  if (isAnon) document.getElementById('btn-upgrade-set').onclick = signInGoogle;
  document.getElementById('set-disc').onclick = () => openDisclaimerModal();
  main.querySelectorAll('[data-lang]').forEach(b => {
    b.onclick = async () => { await loadI18n(b.dataset.lang); pageSettings(); };
  });
  document.getElementById('btn-logout').onclick = () => signOutUser();
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
  if (!state.fbUser || !state.user) {
    document.getElementById('bottom-nav').style.display = 'none';
    renderLanding();
    return;
  }
  document.getElementById('bottom-nav').style.display = 'flex';
  applyI18n();
  switch (state.page) {
    case 'home': pageHome(); break;
    case 'trading': pageTrading(); break;
    case 'wallet': pageWallet(); break;
    case 'referrals': pageReferrals(); break;
    case 'settings': pageSettings(); break;
  }
}

// ─── Counter ─────────────────────────────────────────────────────
let _counterTimer = null, _counterTarget = 0, _counterDisplay = 0;
function startCounter() {
  if (_counterTimer) clearInterval(_counterTimer);
  async function fetchTotal() {
    try {
      const r = await fetch('/api/stats/public').then(x => x.json());
      _counterTarget = r.total_users || 0;
    } catch {}
  }
  fetchTotal();
  setInterval(fetchTotal, 10000);
  _counterTimer = setInterval(() => {
    const el = document.getElementById('counter-num');
    if (!el) { clearInterval(_counterTimer); return; }
    if (_counterDisplay < _counterTarget) {
      const step = Math.max(1, Math.ceil((_counterTarget - _counterDisplay) / 30));
      _counterDisplay = Math.min(_counterTarget, _counterDisplay + step);
      el.textContent = _counterDisplay.toLocaleString();
      el.classList.add('bump');
      setTimeout(() => el.classList.remove('bump'), 400);
    }
  }, 60);
}

// ─── Landing (로그인 X) ──────────────────────────────────────────
function renderLanding() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <section class="counter">
      <div class="eyebrow"><span class="live-dot"></span>${t('home_counter.label')}</div>
      <div class="num" id="counter-num" style="margin-top:10px;">0</div>
      <div class="muted" style="margin-top:8px;">${t('home_counter.subtitle')}</div>
    </section>

    <section style="margin-top:8px;">
      <h1 style="font-size:28px; line-height:1.2;">
        ${t('landing.hero_title_l1')}<br/>${t('landing.hero_title_l2')}
      </h1>
      <p style="margin-bottom:20px;">${t('landing.hero_sub')}</p>
      <button class="btn lg" id="btn-start">${t('landing.cta_start')}</button>
      <div class="muted" style="margin-top:10px; text-align:center;">
        ${t('landing.cta_note')}
      </div>
    </section>

    <h2>${t('landing.how_title')}</h2>
    <div class="how-step">
      <div class="num">01</div>
      <div><div class="ttl">${t('landing.how_1_title')}</div><div class="ds">${t('landing.how_1_desc')}</div></div>
    </div>
    <div class="how-step">
      <div class="num">02</div>
      <div><div class="ttl">${t('landing.how_2_title')}</div><div class="ds">${t('landing.how_2_desc')}</div></div>
    </div>
    <div class="how-step">
      <div class="num">03</div>
      <div><div class="ttl">${t('landing.how_3_title')}</div><div class="ds">${t('landing.how_3_desc')}</div></div>
    </div>

    <h2>${t('landing.diff_title')}</h2>
    <div class="card tight">
      <div class="row" style="align-items:flex-start; gap:14px;">
        <div class="tag-num solid">A</div>
        <div><h3>${t('landing.diff_a_title')}</h3><p>${t('landing.diff_a_desc')}</p></div>
      </div>
    </div>
    <div class="card tight">
      <div class="row" style="align-items:flex-start; gap:14px;">
        <div class="tag-num solid">B</div>
        <div><h3>${t('landing.diff_b_title')}</h3><p>${t('landing.diff_b_desc')}</p></div>
      </div>
    </div>
    <div class="card tight">
      <div class="row" style="align-items:flex-start; gap:14px;">
        <div class="tag-num solid">C</div>
        <div><h3>${t('landing.diff_c_title')}</h3><p>${t('landing.diff_c_desc')}</p></div>
      </div>
    </div>

    <h2>${t('safety.title')}</h2>
    <div class="card tight">
      <div class="row" style="align-items:flex-start; gap:14px;">
        <div class="tag-num">1</div>
        <div><h3>${t('safety.p1_title')}</h3><p>${t('safety.p1_desc')}</p></div>
      </div>
    </div>
    <div class="card tight">
      <div class="row" style="align-items:flex-start; gap:14px;">
        <div class="tag-num">2</div>
        <div><h3>${t('safety.p2_title')}</h3><p>${t('safety.p2_desc')}</p></div>
      </div>
    </div>
    <div class="card tight">
      <div class="row" style="align-items:flex-start; gap:14px;">
        <div class="tag-num">3</div>
        <div><h3>${t('safety.p3_title')}</h3><p>${t('safety.p3_desc')}</p></div>
      </div>
    </div>

    <div style="height:24px;"></div>
    <button class="btn lg" id="btn-start-2">${t('landing.cta_start')}</button>

    <p class="muted" style="text-align:center; margin-top:14px; font-size:11px; line-height:1.5;">
      ${t('landing.risk_note')}
    </p>
    <div style="text-align:center; margin-top:8px;">
      <button class="btn link" id="btn-landing-disc" style="font-size:11px;">${t('disclaimer.full_title')} →</button>
    </div>

    <footer style="margin:32px 0 16px; text-align:center; font-size:11px; color:var(--text-3); line-height:1.6;">
      <div>${t('disclaimer.footer_short')}</div>
      <div style="margin-top:4px;">© STREAK · <a href="https://github.com/yujoohwan6342-stack/POLY" target="_blank" rel="noopener" style="color:var(--text-3); text-decoration:underline;">GitHub</a></div>
    </footer>
  `;
  document.getElementById('btn-start').onclick = openSignInModal;
  const startBtn2 = document.getElementById('btn-start-2');
  if (startBtn2) startBtn2.onclick = openSignInModal;
  document.getElementById('btn-landing-disc').onclick = () => openDisclaimerModal();
  startCounter();
}

function openSignInModal() {
  const overlay = document.createElement('div');
  overlay.style.cssText = `position:fixed; inset:0; background:rgba(0,0,0,0.5);
    display:flex; align-items:center; justify-content:center; z-index:1000;
    padding:24px; backdrop-filter:blur(4px);`;
  overlay.innerHTML = `
    <div style="background:var(--bg); border-radius:20px; padding:28px;
                max-width:360px; width:100%; box-shadow:var(--shadow-lg);">
      <div style="text-align:center; font-size:40px; margin-bottom:8px;">🚀</div>
      <h2 style="margin:0 0 6px; text-align:center;">${t('auth.choose_method')}</h2>
      <p style="text-align:center; color:var(--text-3); margin:0 0 20px; font-size:13px;">
        ${t('auth.choose_desc')}
      </p>
      <div class="col" style="gap:8px;">
        <button class="btn" id="m-google" style="background:#fff; color:#000; border:1px solid var(--border);">
          <span style="font-size:18px;">G</span> ${t('auth.google')} (+${state.config.signup_bonus_google})
        </button>
        <button class="btn ghost" id="m-anon">
          👤 ${t('auth.anonymous')} (+${state.config.signup_bonus_anon})
        </button>
        <button class="btn ghost" id="m-cancel" style="margin-top:6px;">${t('auth.later')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  overlay.querySelector('#m-cancel').onclick = close;
  overlay.querySelector('#m-anon').onclick = async () => { close(); await signInAnon(); };
  overlay.querySelector('#m-google').onclick = async () => { close(); await signInGoogle(); };
}

// ─── Init ────────────────────────────────────────────────────────
async function init() {
  await loadI18n(state.lang);
  state.config = await fetch('/api/config').then(r => r.json());

  document.querySelectorAll('#lang-switch button').forEach(b => {
    b.onclick = () => loadI18n(b.dataset.lang).then(render);
  });
  document.querySelectorAll('#bottom-nav a').forEach(a => {
    a.onclick = (e) => { e.preventDefault(); navigate(a.dataset.page); };
  });

  render();  // 첫 화면 (랜딩)

  if (state.config.firebase && state.config.firebase.apiKey) {
    await initFirebase(state.config.firebase);
  } else {
    showToast('Firebase config missing — server admin must set FIREBASE_API_KEY env');
  }
}

init().catch(e => { console.error(e); showToast(t('common.error')); });
})();
