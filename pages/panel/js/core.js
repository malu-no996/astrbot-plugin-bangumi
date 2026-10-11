/* 番剧面板 - 公共部分（bridge / 工具 / 全局状态 / 概览加载）
 * 子页签各自一个文件：js/calendar.js（每日放送）、js/subs.js（我的订阅 + 全部浮层），
 * 入口与事件分发在 js/main.js。经典脚本按序加载，顶层 const/函数全局可见。
 */
const B = window.AstrBotPluginPage;

/* ---------------- 工具 ---------------- */

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmt(ts) {
  ts = Number(ts || 0);
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function short(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n) + '…' : s;
}
/* 封面直连（bangumi-data 走 AniList / B站 CDN）：no-referrer 绕过防盗链 */
function img(url, cls, alt) {
  if (!url) return '';
  return `<img src="${esc(url)}" referrerpolicy="no-referrer" class="${cls || ''}" alt="${esc(alt || '')}" loading="lazy">`;
}
function openImg(url) { if (url) window.open(url, '_blank'); }

async function GET(path, params) {
  try { return await B.apiGet(path, params || {}); }
  catch (e) { return { ok: false, message: e.message }; }
}
async function POST(path, body) {
  try { return await B.apiPost(path, body || {}); }
  catch (e) { return { ok: false, message: e.message }; }
}

function notify(msg, type) {
  const box = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (type || '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}
function confirmBox(msg) { return window.confirm(msg); }

/* 两段式确认：受限 iframe 里 window.confirm 被禁（静默返回 false），
 * 改成「点一下按钮变『确认？』，再点一下才执行」。返回 true = 已确认可执行。 */
function markConfirm(el) {
  if (!el) return true;
  if (el.dataset.armed === '1') {
    el.dataset.armed = '';
    return true;
  }
  el.dataset.label = el.textContent;
  el.dataset.armed = '1';
  el.textContent = '确认？';
  setTimeout(() => {
    if (el.dataset.armed === '1') { el.dataset.armed = ''; el.textContent = el.dataset.label || '删除'; }
  }, 3000);
  return false;
}

/* ---------------- 全局状态 ---------------- */

const S = {
  tab: 'calendar',
  weekdays: [],            // [{key:1,label:'周一'}, …]
  providers: [],           // 数据源下拉
  pushStatus: '',
  counts: {},
  settings: { enabled: true, provider: 'bdata', owner_only: false },
  calendar: {
    days: [], loading: false, cached: false, fetched_at: 0,
    q: '', results: [], searching: false, source: '', error: '',
    guide: false, ttl_hint: '',
  },
  rules: [],               // 全部订阅（规则：一条 = 一部番剧 + 一个推送目标）
  targets: { instances: [], loading: false },
  form: null,              // 编辑中的订阅（新增 / 编辑共用）
  picks: { group: '' },    // 群下拉的选中值（真正写进规则的是 form.target_id）
  bulk: {
    open: false, weekdays: [], instance_id: '', group: '',
    target_id: '', target_name: '', time_mode: 'own', time: '23:00',
    delay_minutes: 10, submitting: false,
  },
  bulkDaysLoaded: false,
  preview: { show: false, text: '' },
  confirm: null,           // 二次确认浮层：{title, ids, tip, kind, pid, gid, doing}
  undo: [],                // 上次批量订阅新增的 id（一键撤销）
  allSel: false,
  /* --- 订阅数据页（群订阅） --- */
  gsubs: { list: [], counts: {}, loading: false },   // 一行 = 一个群
  subAdd: null,            // 手工新增 / 编辑群订阅的表单
  anime: {                 // 某个群的番剧订阅浮层（pid/gid 为准，不用对象快照免得过期）
    open: false, pid: '', gid: '', item: '', q: '', results: [], searching: false, busy: false,
  },
};

/* 按「a.b.c」路径读/写状态（事件委托绑定用） */
function stateGet(path) {
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), S);
}
function stateSet(path, value) {
  const keys = path.split('.');
  let o = S;
  for (let i = 0; i < keys.length - 1; i++) o = o[keys[i]];
  o[keys[keys.length - 1]] = value;
}

/* ---------------- 星期 ---------------- */

/* 卡片角标用的单字星期：1→一 … 7→日 */
const WD_CHAR = { 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '日' };
function wdChar(key) { return WD_CHAR[Number(key)] || ''; }
function wdLabel(key) {
  const hit = S.weekdays.find((w) => String(w.key) === String(key));
  return hit ? hit.label : String(key || '');
}
/* label 已带「周」时别再拼（后端 store.WEEKDAY_LABELS 就是「周二」这种） */
function wdText(key) {
  const t = String(wdLabel(key) || '');
  return t.startsWith('周') ? t : '周' + t;
}

function ruleById(id) {
  return S.rules.find((r) => String(r.id) === String(id)) || null;
}

/* ---------------- 概览加载 ---------------- */

async function loadState() {
  const j = await GET('state');
  if (!j.ok) { notify(j.message || '加载失败', 'err'); return; }
  S.weekdays = j.weekdays || [];
  S.providers = j.providers || [];
  S.pushStatus = j.push_status || '';
  S.counts = j.counts || {};
  S.settings = Object.assign({ enabled: true, provider: 'bdata' }, j.settings || {});
  const hours = Number(j.calendar_ttl_hours || 24);
  S.calendar.ttl_hint = hours >= 24 && hours % 24 === 0
    ? `每 ${hours / 24} 天自动拉一次`
    : `每 ${hours} 小时自动拉一次`;
}
