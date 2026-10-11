/* 番剧面板 - 入口：页签 / 渲染 / 事件分发 / 启动
 * 子页签逻辑见同目录：core.js（公共）、calendar.js（每日放送）、subs.js（我的订阅 + 浮层）。
 * 经典脚本按 index.html 里的顺序加载，顶层 const/函数全局共享。
 */

const TABS = [['calendar', '每日放送'], ['subs', '我的订阅']];

function render() {
  // 页签
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = TABS.map(([id, label]) => {
    const cnt = id === 'subs' && S.rules.length ? ` <span class="badge online" style="margin-left:4px">${S.rules.length}</span>` : '';
    return `<span class="tab ${S.tab === id ? 'active' : ''}" data-act="switchTab" data-arg="${id}">${label}${cnt}</span>`;
  }).join('');
  // 模块开关徽标 + 勾选框状态（头部是静态 DOM，只在原地同步）
  const badge = document.getElementById('mod-badge');
  if (badge) {
    badge.textContent = S.settings.enabled ? '模块已启用' : '模块已禁用';
    badge.className = 'badge ' + (S.settings.enabled ? 'online' : 'offline');
  }
  const sw = document.querySelector('input[data-model="settings.enabled"]');
  if (sw) sw.checked = !!S.settings.enabled;
  // 内容
  const view = document.getElementById('view');
  const map = { calendar: renderCalendar, subs: renderSubs };
  view.innerHTML = (map[S.tab] || renderCalendar)();
  renderModal();
}

/* 切换子页签（进入时才懒加载） */
function switchTab(id) {
  S.tab = id;
  if (id === 'calendar' && !S.calendar.days.length && !S.calendar.loading) calLoad(false);
  if (id === 'subs') { subsLoad(); if (!S.targets.instances.length) subsTargets(); }
  render();
}

/* ---------------- 模块开关 / 设置 ---------------- */

async function saveModule() {
  const j = await POST('settings', { enabled: !!S.settings.enabled, provider: S.settings.provider });
  if (!j.ok) { notify(j.message || '保存失败', 'err'); await loadState(); render(); return; }
  notify(S.settings.enabled ? '模块已启用（后台推送生效）' : '模块已禁用（后台推送空转）', 'ok');
  if (j.settings) S.settings = Object.assign(S.settings, j.settings);
  await loadState();
  render();
}

/* ---------------- 动作分发（事件委托） ---------------- */

const ACTIONS = {
  switchTab: (id) => switchTab(id),
  // 每日放送
  calSearch, calRefresh, calToggleGuide, calClearResults,
  calSubscribe: (key) => calSubscribe(key),
  calProviderChanged,
  // 订阅
  subsEditNew,
  subsEditOpen: (id) => subsEditOpen(id),
  subsSave, subsCancel,
  subsToggle: (id) => subsToggle(id),
  subsDelete: (id, el) => subsDelete(id, el),
  subsRun: (id) => subsRun(id),
  subsPreview: (id) => subsPreview(id),
  subsPreviewClose,
  selToggleAll, selReset,
  subsBatchEnable: () => subsBatch('enable'),
  subsBatchDisable: () => subsBatch('disable'),
  subsBatchDelete: () => subsBatch('delete'),
  undoOpen, confirmClose, confirmDo,
  // 批量订阅浮层
  bulkOpen, bulkClose, bulkToggleWeekday: (k) => bulkToggleWeekday(k),
  bulkWeekdaysAll, bulkSubmit,
  // data-after（下拉/勾选框改变后）
  saveModule, pickInstance, pickGroup, pickTargetType,
  bulkPickInstance, bulkPickGroup,
};

document.addEventListener('click', (e) => {
  // 点浮层遮罩空白处 = 关闭（只有点在遮罩本体上才算）
  const ov = e.target.closest('[data-overlay-act]');
  if (ov && e.target === ov) {
    const fn = ACTIONS[ov.dataset.overlayAct];
    if (fn) fn();
    return;
  }
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) { e.preventDefault(); fn(el.dataset.arg, el); }   // 第二参 = 按钮元素（两段式确认用）
});

/* 输入框：同步进状态（不重渲染，免得把正在输入的内容冲掉） */
document.addEventListener('input', (e) => {
  const el = e.target;
  if (el.dataset.model) {
    stateSet(el.dataset.model, el.type === 'checkbox' ? el.checked : el.value);
  }
});

/* 下拉框 / 勾选框：同步进状态，然后可选地跑一个动作（data-after）再重渲染 */
document.addEventListener('change', (e) => {
  const el = e.target;
  if (el.dataset.sel) {          // 订阅行勾选
    selRowChanged(el.dataset.sel, el);
    return;
  }
  if (el.dataset.model) {
    stateSet(el.dataset.model, el.type === 'checkbox' ? el.checked : el.value);
    if (el.dataset.after && ACTIONS[el.dataset.after]) ACTIONS[el.dataset.after](el.dataset.arg, el);
    else render();
  }
});

/* 回车触发所在输入框的主动作（data-enter） */
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const el = e.target;
  if (el.tagName !== 'INPUT') return;
  const act = el.dataset.enter;
  if (act && ACTIONS[act]) { e.preventDefault(); ACTIONS[act](); }
});

/* ---------------- 启动 ---------------- */

(async () => {
  const ctx = await B.ready();
  document.title = ctx.pageTitle || '番剧面板';
  render();
  await loadState();
  render();
  if (S.tab === 'calendar') calLoad(false);
})();
