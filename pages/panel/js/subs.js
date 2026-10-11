/* 子页签：我的订阅（原 frag/subs）—— 一条订阅 = 一部番剧 + 一个推送目标 + 每周几几点
 * ------------------------------------------------------------------
 * 几个「看起来像 bug、其实是设计」的点：
 * - 群 vs 私聊：只有 OneBot 能按 QQ 号发私聊，QQ 官方机器人拿不到用户 openid，
 *   所以选了官方实例时「私聊」直接禁用（页面显示原因），避免配了永远推不出去。
 * - 官方机器人的群：AstrBot 没有「所在群列表」接口 → 群 ID 手填（群消息事件里的 session_id）。
 * - 「立即推送」/「预览」只是看效果：**预览不真发**，「立即推送」真发一条。
 * - 编辑浮层由日历页的「订阅」按钮跨文件调 subsEditOpenItem 打开（浮层挂在 view 之外）。
 * - 破坏性操作（批量删除 / 撤销）走页内确认浮层，不用 window.confirm。
 */

const DEFAULT_BULK_TIME = '23:00';

/* ---------------- 表单 ---------------- */

function blankForm() {
  return {
    id: '',
    subject_id: 0,
    provider: '',         // 空 = 跟随当前数据源设置
    bili_id: '',          // B站 season_id（有就按 B站时间表定集数，最准）
    title: '',
    native: '',
    image: '',
    url: '',
    air_date: '',
    total_episodes: 0,
    weekday: 1,
    time: '23:00',        // 数据源一般会给；手填时给个日本深夜番常见的北京时间作起点
    interval_days: 7,     // 几集之间隔几天（双周更填 14）
    delay_minutes: 10,
    platform_id: '',
    platform_name: '',
    target_type: 'group',
    target_id: '',
    target_name: '',
    template: '',
    enabled: true,
  };
}

/* 「YYYY-MM-DD」→ 周几（1=周一 … 7=周日）。
 * ★ 必须按年月日手动构造 Date：`new Date('2026-10-05')` 按 UTC 解析，
 *   东八区取 getDay() 会拿到前一天，订阅就整体错开一天。 */
function weekdayOf(airDate) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(airDate || '').trim());
  if (!m) return 1;
  const w = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getDay();
  return w === 0 ? 7 : w;
}

/* ---------------- 数据 ---------------- */

async function subsLoad() {
  const j = await GET('rules');
  if (!j.ok) { notify(j.message || '订阅列表加载失败', 'err'); return; }
  S.rules = j.rules || [];
  S.allSel = false;     // 列表换了新对象，勾选状态也随之清空
  render();
}

async function subsTargets() {
  S.targets.loading = true;
  const j = await GET('targets');
  S.targets.loading = false;
  if (!j.ok) {
    S.targets.instances = j.instances || [];
    if (j.message) notify(j.message, 'err');
    return;
  }
  S.targets.instances = j.instances || [];
}

function instanceById(id) {
  return S.targets.instances.find((x) => String(x.id) === String(id)) || null;
}
function groupsOf(id) {
  const ins = instanceById(id);
  return ins ? (ins.groups || []).map((g) => ({ id: String(g.group_id), name: String(g.group_name || g.group_id) })) : [];
}
function allowPrivate() {
  const ins = instanceById(S.form && S.form.platform_id);
  return !!(ins && ins.private);
}

/* ---------------- 编辑浮层 ---------------- */

async function subsEditOpen(id) {
  const rule = ruleById(id);
  if (!rule) return;
  await subsEditOpenItem(rule, true);
}

async function subsEditOpenItem(src, isRule) {
  if (!S.targets.instances.length) await subsTargets();
  if (isRule) {
    S.form = Object.assign(blankForm(), {
      id: String(src.id || ''),
      subject_id: Number(src.subject_id || 0),
      provider: String(src.provider || ''),
      bili_id: String(src.bili_id || ''),
      title: String(src.title || ''),
      native: String(src.native || ''),
      image: String(src.image || ''),
      url: String(src.url || ''),
      air_date: String(src.air_date || ''),
      total_episodes: Number(src.total_episodes || 0),
      weekday: Number(src.weekday || 1),
      time: String(src.time || '23:00'),
      interval_days: Number(src.interval_days || 7),
      delay_minutes: Number(src.delay_minutes || 0),
      platform_id: String(src.platform_id || ''),
      platform_name: String(src.platform_name || ''),
      target_type: String(src.target_type || 'group'),
      target_id: String(src.target_id || ''),
      target_name: String(src.target_name || ''),
      template: String(src.template || ''),
      enabled: src.enabled !== false,
    });
    S.picks.group = String(src.target_id || '');
  } else {
    // 从放送表/搜索结果过来：数据源已经给了「周几 + 几点」，直接带上，别让人再填一遍
    S.form = Object.assign(blankForm(), {
      subject_id: Number(src.subject_id || 0),
      title: String(src.title || src.native || ''),
      native: String(src.native || ''),
      image: String(src.image || ''),
      url: String(src.url || ''),
      air_date: String(src.air_date || ''),
      total_episodes: Number(src.total || 0),
      weekday: Number(src.weekday || weekdayOf(src.air_date)) || 1,
      time: String(src.time || '23:00'),
      interval_days: Number(src.interval || 7) || 7,
      bili_id: String(src.bili_id || ''),
      provider: String(src.provider || ''),
    });
    S.picks.group = '';
  }
  render();
}

function subsCancel() { S.form = null; render(); }

function pickInstance() {
  const f = S.form;
  if (!f) return;
  const ins = instanceById(f.platform_id);
  f.platform_name = ins ? (ins.name || ins.id) : '';
  S.picks.group = '';
  f.target_id = '';
  f.target_name = '';
  if (ins && !ins.private) f.target_type = 'group';   // 官方机器人不支持私聊
  render();
}

function pickGroup() {
  const hit = groupsOf(S.form && S.form.platform_id).find((g) => g.id === S.picks.group);
  if (hit) {
    S.form.target_id = hit.id;
    S.form.target_name = hit.name;
  }
  render();
}

async function subsSave() {
  const f = S.form;
  if (!f) return;
  if (!String(f.title || '').trim()) { notify('番剧名为空', 'err'); return; }
  if (!String(f.platform_id || '').trim()) { notify('请选择推送用的平台实例', 'err'); return; }
  if (!String(f.target_id || '').trim()) { notify('请选择群 / 填写私聊用户 ID', 'err'); return; }
  if (!/^\d{1,2}:\d{2}$/.test(String(f.time || ''))) {
    notify('播出时刻格式要是 HH:MM，例如 23:00', 'err');
    return;
  }
  const payload = Object.assign({}, f, {
    total_episodes: Number(f.total_episodes || 0),
    weekday: Number(f.weekday || 1),
    interval_days: Number(f.interval_days || 7),
    delay_minutes: Number(f.delay_minutes || 0),
  });
  const j = await POST('rule/save', payload);
  if (!j.ok) { notify(j.message || '保存失败', 'err'); return; }
  notify(j.message || '已保存', 'ok');
  S.form = null;
  S.tab = 'subs';        // 从日历页订阅过来的，存完直接跳到订阅列表看结果
  subsLoad();
  loadState();
}

/* ---------------- 订阅列表操作 ---------------- */

async function subsToggle(id) {
  const r = ruleById(id);
  if (!r) return;
  const j = await POST('rule/toggle', { id: r.id, enabled: !r.enabled });
  notify(j.message || (j.ok ? '已切换' : '切换失败'), j.ok ? 'ok' : 'err');
  if (j.ok) subsLoad();
}

async function subsDelete(id, el) {
  if (!markConfirm(el)) return;    // 第一次点 = 变「确认？」，再点一次才真删
  const j = await POST('rule/delete', { id });
  notify(j.message || (j.ok ? '已删除' : '删除失败'), j.ok ? 'ok' : 'err');
  if (j.ok) { subsLoad(); loadState(); }
}

async function subsRun(id) {
  const j = await POST('rule/run', { id });
  notify(j.message || (j.ok ? '已推送' : '推送失败'), j.ok ? 'ok' : 'err');
  subsLoad();
}

async function subsPreview(id) {
  const j = await GET('rule/preview', { id });
  if (!j.ok) { notify(j.message || '预览失败', 'err'); return; }
  S.preview.text = j.text || '（今天没有可推送的集数）';
  S.preview.show = true;
  render();
}
function subsPreviewClose() { S.preview.show = false; render(); }

/* ---------------- 勾选 / 批量操作 ---------------- */

function selIds() { return S.rules.filter((r) => r._sel).map((r) => String(r.id)); }
function selCount() { return selIds().length; }

function selToggleAll() {
  S.allSel = !S.allSel;
  S.rules.forEach((r) => { r._sel = S.allSel; });
  render();
}
function selReset() {
  S.allSel = false;
  S.rules.forEach((r) => { r._sel = false; });
  render();
}
function selRowChanged(id, el) {
  const r = ruleById(id);
  if (r) r._sel = !!(el && el.checked);
  S.allSel = S.rules.length > 0 && S.rules.every((r) => r._sel);
  render();
}

function confirmOpen(title, ids, tip) {
  S.confirm = { title, ids: ids.slice(), tip: tip || '', doing: false };
  render();
}
function confirmClose() { S.confirm = null; render(); }

async function confirmDo() {
  if (!S.confirm) return;
  S.confirm.doing = true; render();
  const j = await POST('rule/batch', { action: 'delete', ids: S.confirm.ids });
  if (!j.ok) { notify(j.message || '删除失败', 'err'); S.confirm.doing = false; render(); return; }
  notify(j.message || '已删除', 'ok');
  const gone = new Set(S.confirm.ids.map(String));
  S.undo = S.undo.filter((i) => !gone.has(String(i)));
  S.confirm = null;
  S.allSel = false;
  subsLoad();
  loadState();
}

async function subsBatch(action) {
  const ids = selIds();
  if (!ids.length) { notify('先勾选要操作的订阅', 'err'); return; }
  if (action === 'delete') {
    confirmOpen('删除选中的订阅', ids, `将要删除 ${ids.length} 条订阅，删除后无法恢复。`);
    return;
  }
  const j = await POST('rule/batch', { action, ids });
  if (!j.ok) { notify(j.message || '操作失败', 'err'); return; }
  notify(j.message || '已更新', 'ok');
  S.allSel = false;
  subsLoad();
  loadState();
}

function undoOpen() {
  if (!S.undo.length) { notify('没有可撤销的批量订阅', 'err'); return; }
  confirmOpen('撤销上次批量订阅', S.undo, `这次批量订阅新增了 ${S.undo.length} 条，将全部删除。`);
}

/* ---------------- 批量订阅 ---------------- */

async function bulkOpen() {
  if (!S.targets.instances.length) await subsTargets();
  // 放送表可能还没拉过（子页是懒加载的，不去「每日放送」就不会加载）。
  // 这里补一次**只读缓存**的加载：弹浮层不该触发外部请求。
  if (!(S.calendar.days || []).length) await calLoad(false);
  S.bulk.open = true;
  render();
}
function bulkClose() { S.bulk.open = false; render(); }

function bulkPickInstance() {
  const b = S.bulk;
  b.group = '';
  b.target_id = '';
  b.target_name = '';
  render();
}
function bulkPickGroup() {
  const b = S.bulk;
  const hit = groupsOf(b.instance_id).find((g) => g.id === b.group);
  if (hit) {
    b.target_id = hit.id;
    b.target_name = hit.name;
  }
  render();
}

function bulkToggleWeekday(key) {
  const k = Number(key);
  const i = S.bulk.weekdays.indexOf(k);
  if (i >= 0) S.bulk.weekdays.splice(i, 1);
  else S.bulk.weekdays.push(k);
  render();
}
function bulkWeekdaysAll() { S.bulk.weekdays = []; render(); }
function bulkSelectedWeekday(key) { return S.bulk.weekdays.includes(Number(key)); }

/* 按当前过滤条件铺平成一条条番剧（带出所属周几） */
function bulkItems() {
  const b = S.bulk;
  const out = [];
  for (const d of (S.calendar.days || [])) {
    if (b.weekdays.length && !b.weekdays.includes(Number(d.weekday))) continue;
    for (const it of (d.items || [])) {
      out.push(Object.assign({}, it, { weekday: Number(it.weekday || d.weekday) || Number(d.weekday) }));
    }
  }
  return out;
}

/* 同一部番 + 同一个目标才算重复（换一个群仍会加：不同群各推一份是正常需求） */
function _bulkKey(it) {
  return String(it.subject_id || '') ? 'id:' + it.subject_id : 't:' + String(it.title || '').trim();
}
function bulkPlan() {
  const b = S.bulk;
  const items = bulkItems();
  if (!b.instance_id || !b.target_id) return { add: [], skip: [], items };
  const have = new Set(
    S.rules
      .filter((r) => String(r.platform_id) === String(b.instance_id) && String(r.target_id) === String(b.target_id))
      .map(_bulkKey)
  );
  const add = [], skip = [];
  for (const it of items) (have.has(_bulkKey(it)) ? skip : add).push(it);
  return { add, skip, items };
}
function bulkTotalCount() { return bulkItems().length; }
function bulkAddCount() { return bulkPlan().add.length; }
function bulkSkipCount() { return bulkPlan().skip.length; }

async function bulkSubmit() {
  const b = S.bulk;
  if (!b.instance_id) { notify('请选择推送用的平台实例', 'err'); return; }
  if (!b.target_id) { notify('请选择要推送到的群 / 填写群 ID', 'err'); return; }
  const plan = bulkPlan();
  if (!plan.add.length) { notify('这个群已经订阅过这些番剧了', 'err'); return; }
  b.submitting = true; render();
  const j = await POST('rules/bulk', {
    platform_id: b.instance_id,
    target_id: b.target_id,
    target_name: b.target_name,
    platform_name: String((instanceById(b.instance_id) || {}).name || b.instance_id),
    weekdays: b.weekdays,
    time_mode: b.time_mode,
    time: b.time,
    delay_minutes: Number(b.delay_minutes || 0),
    target_type: 'group',
  });
  b.submitting = false;
  if (!j.ok) { notify(j.message || '批量订阅失败', 'err'); render(); return; }
  notify(j.message || '已批量订阅', 'ok');
  S.undo = (j.ids || []).map(String);   // 反悔时一键删掉，不用再勾几十行
  S.confirm = null;
  b.open = false;
  subsLoad();
  loadState();
}

/* ---------------- 展示辅助 ---------------- */

function subsTargetName(r) {
  if (r.target_type === 'private') return `私聊 ${r.target_id}`;
  return String(r.target_name || r.target_id || '（未选择）');
}
function subsWhenText(r) {
  const t = String(r.time || '00:00');
  const d = Number(r.delay_minutes || 0);
  return `${wdText(r.weekday)} ${t}${d ? `（延后 ${d} 分钟）` : ''}`;
}
function subsLastText(r) {
  if (!r.last_run_at) return '还没推过';
  const d = new Date(Number(r.last_run_at) * 1000);
  const ep = Number(r.last_episode || 0);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}` + (ep ? ` 第 ${ep} 集` : '');
}

/* ---------------- 渲染 ---------------- */

function renderSubs() {
  const sb = S.rules;
  let h = `<div class="card">`;

  h += `<div class="toolbar">
    <button data-act="subsEditNew">+ 手动添加订阅</button>
    <button class="ghost" data-act="bulkOpen" title="把放送表里的一批番剧一次性订阅到同一个群">批量订阅到同一个群</button>
    ${S.undo.length ? `<button class="ghost sm" data-act="undoOpen" title="删掉上一次「批量订阅」新增的那几十条">撤销上次批量订阅（${S.undo.length} 条）</button>` : ''}
    <span class="muted">${esc(S.pushStatus || '')}</span>
  </div>`;

  // 批量操作条：勾了行才出现
  if (selCount()) {
    h += `<div class="bg-bar"><b>已选 ${selCount()} 条</b>
      <button class="bg-btn-xs" data-act="subsBatchEnable">启用</button>
      <button class="bg-btn-xs" data-act="subsBatchDisable">停用</button>
      <button class="bg-btn-xs bg-btn-danger" data-act="subsBatchDelete">删除所选</button>
      <button class="bg-btn-xs" data-act="selReset">取消选择</button>
    </div>`;
  }

  if (sb.length) {
    h += `<table class="bg-table"><thead><tr>
      <th style="width:30px"><input type="checkbox" data-act="selToggleAll"${S.allSel ? ' checked' : ''}></th>
      <th style="width:26%">番剧</th><th style="width:14%">播出</th>
      <th style="width:18%">推送到</th><th style="width:14%">上次推送</th><th>状态 / 操作</th>
    </tr></thead><tbody>`;
    for (const r of sb) {
      h += `<tr>
        <td><input type="checkbox" data-sel="${esc(String(r.id))}"${r._sel ? ' checked' : ''}></td>
        <td>
          <div class="bg-cell-title" title="${esc(r.native)}">${esc(r.title || '（未命名）')}</div>
          ${r.native && r.native !== r.title ? `<div class="bg-cell-sub">${esc(r.native)}</div>` : ''}
          ${r.air_date ? `<div class="bg-cell-sub">首播 ${esc(r.air_date)}${r.total_episodes ? ` · 全 ${Number(r.total_episodes)} 集` : ''}</div>` : ''}
        </td>
        <td>${esc(subsWhenText(r))}</td>
        <td><div>${esc(subsTargetName(r))}</div><div class="bg-cell-sub">${esc(r.platform_name || r.platform_id || '（未选实例）')}</div></td>
        <td>${esc(subsLastText(r))}</td>
        <td>
          <span class="badge ${r.enabled ? 'online' : 'offline'}">${r.enabled ? '启用' : '停用'}</span>
          <button class="bg-btn-xs" data-act="subsToggle" data-arg="${esc(String(r.id))}">${r.enabled ? '停用' : '启用'}</button>
          <button class="bg-btn-xs" data-act="subsEditOpen" data-arg="${esc(String(r.id))}">编辑</button>
          <button class="bg-btn-xs" data-act="subsPreview" data-arg="${esc(String(r.id))}">预览</button>
          <button class="bg-btn-xs" data-act="subsRun" data-arg="${esc(String(r.id))}">立即推送</button>
          <button class="bg-btn-xs bg-btn-danger" data-act="subsDelete" data-arg="${esc(String(r.id))}">删除</button>
          ${r.last_result ? `<div class="bg-cell-sub">${esc(r.last_result)}</div>` : ''}
        </td>
      </tr>`;
    }
    h += `</tbody></table>`;
  } else {
    h += `<p class="muted">还没有订阅。去「每日放送」里挑一部点「订阅」，或者点上面的「手动添加订阅」。</p>`;
  }

  h += `<div class="guide">
    <p class="guide-title">推送时机怎么算</p>
    <ul class="guide-list">
      <li>到「每周几的几点 + 延迟分钟」就发一条，内容是「《番剧名》第 N 集更新了」</li>
      <li><b>第 N 集</b>：填了 B站 season_id 就查 B站时间表当天那集（最准，能看出停播/连播）；
          否则按首播日 + 间隔天数推算（双周更也认）</li>
      <li>推算集数超过<b>总集数</b>就认为已完结，自动跳过（总集数留 0 = 不限制）</li>
      <li><b>本周那个时刻已经过去</b>时排到下一周，不会补推 —— 想立刻看效果请用「立即推送」</li>
    </ul>
  </div></div>`;

  return h;
}

/* ---------------- 浮层 ---------------- */

function renderModal() {
  const box = document.getElementById('modal');
  let h = '';
  if (S.form) h += modalForm();
  if (S.bulk.open) h += modalBulk();
  if (S.confirm) h += modalConfirm();
  if (S.preview.show) h += modalPreview();
  box.innerHTML = h;
}

function modalForm() {
  const f = S.form;
  const groups = groupsOf(f.platform_id);
  const privateOk = allowPrivate();
  let targetField;
  if (f.target_type === 'group') {
    targetField = groups.length
      ? `<label class="bg-field"><span>群</span>
           <select class="bg-input" data-model="picks.group" data-after="pickGroup">
             <option value="">请选择</option>
             ${groups.map((g) => `<option value="${esc(g.id)}"${String(f.target_id) === g.id ? ' selected' : ''}>${esc(g.name)}</option>`).join('')}
           </select></label>`
      : `<label class="bg-field"><span>群 ID（该平台群消息事件里的 session_id）</span>
           <input class="bg-input" data-model="form.target_id" value="${esc(f.target_id)}" placeholder="如 123456789"></label>`;
  } else {
    targetField = `<label class="bg-field"><span>接收人用户 ID</span>
        <input class="bg-input" data-model="form.target_id" value="${esc(f.target_id)}" placeholder="私聊仅 OneBot 支持"></label>`;
  }
  return `<div class="modal" data-overlay-act="subsCancel">
    <div class="modal-box">
      <h3>${f.id ? '编辑订阅' : '新增订阅'}</h3>
      ${f.image
        ? `<div class="bg-form-head">
             <img class="bg-cover" src="${esc(f.image)}" referrerpolicy="no-referrer" alt="${esc(f.title)}">
             <div style="flex:1 1 auto;min-width:0">
               <input class="bg-input" data-model="form.title" value="${esc(f.title)}" placeholder="番剧名（必填）">
               ${f.native ? `<div class="bg-cell-sub">原名 ${esc(f.native)}</div>` : ''}
             </div>
           </div>`
        : `<label class="bg-field bg-field-wide"><span>番剧名（必填）</span>
             <input class="bg-input" data-model="form.title" value="${esc(f.title)}" placeholder="必填"></label>`}

      <div class="row">
        <label class="bg-field"><span>首播日（推算第几集要用）</span>
          <input class="bg-input" data-model="form.air_date" value="${esc(f.air_date)}" placeholder="2026-10-05"></label>
        <label class="bg-field"><span>每周几播出</span>
          <select class="bg-input" data-model="form.weekday">
            ${S.weekdays.map((w) => `<option value="${w.key}"${Number(f.weekday) === Number(w.key) ? ' selected' : ''}>${esc(w.label)}</option>`).join('')}
          </select></label>
        <label class="bg-field"><span>播出时刻（HH:MM）</span>
          <input class="bg-input" data-model="form.time" value="${esc(f.time)}" placeholder="23:00"></label>
        <label class="bg-field"><span>延后几分钟推</span>
          <input class="bg-input" type="number" min="0" max="1440" data-model="form.delay_minutes" value="${esc(f.delay_minutes)}"></label>
        <label class="bg-field"><span>总集数（0 = 不限）</span>
          <input class="bg-input" type="number" min="0" data-model="form.total_episodes" value="${esc(f.total_episodes)}"></label>
        <label class="bg-field"><span>几集隔几天（双周更填 14）</span>
          <input class="bg-input" type="number" min="1" max="365" data-model="form.interval_days" value="${esc(f.interval_days)}"></label>
      </div>
      <label class="bg-field bg-field-wide"><span>B站 season_id（选填，填了就按 B站时间表定集数，最准）</span>
        <input class="bg-input" data-model="form.bili_id" value="${esc(f.bili_id)}" placeholder="留空 = 按首播日 + 周期推算"></label>
      <p class="bg-cell-sub"><b>首播日</b>用来推算第几集：<code>(今天 - 首播日) // 间隔天数 + 1</code>（默认 7 天一集）。
        留空又查不到剧集表 → 算不出集数，这条订阅就不会发消息。</p>

      <div class="row">
        <label class="bg-field"><span>用哪个平台实例</span>
          <select class="bg-input" data-model="form.platform_id" data-after="pickInstance">
            <option value="">请选择</option>
            ${S.targets.instances.map((ins) => `<option value="${esc(ins.id)}"${String(f.platform_id) === String(ins.id) ? ' selected' : ''}>${esc(ins.name)}（${ins.protocol === 'onebot' ? 'OneBot' : (ins.protocol === 'qq_official' ? '官方' : ins.protocol)}）</option>`).join('')}
          </select></label>
        <label class="bg-field"><span>发到哪</span>
          <select class="bg-input" data-model="form.target_type" data-after="pickTargetType">
            <option value="group"${f.target_type === 'group' ? ' selected' : ''}>群</option>
            <option value="private"${f.target_type === 'private' ? ' selected' : ''}${privateOk ? '' : ' disabled'}>私聊（仅 OneBot）</option>
          </select></label>
        ${targetField}
      </div>
      ${privateOk ? '' : '<p class="muted">当前实例是 QQ 官方机器人 / 其它平台：拿不到用户 openid，只能发群。</p>'}

      <label class="bg-field bg-field-wide"><span>自定义消息模板（留空用默认排版）</span>
        <input class="bg-input" data-model="form.template" value="${esc(f.template)}"
               placeholder="【新番更新】《{title}》第 {ep} 集更新了"></label>
      <p class="bg-cell-sub">可用占位符：{title} 番剧名 · {native} 原名 · {ep} 第几集 · {total} 总集数 ·
        {weekday} 周几 · {time} 时刻 · {date} 日期 · {url} 条目链接</p>

      <div class="actions">
        <button data-act="subsCancel">取消</button>
        <button data-act="subsSave">保存</button>
      </div>
    </div>
  </div>`;
}

function modalBulk() {
  const b = S.bulk;
  const groups = groupsOf(b.instance_id);
  return `<div class="modal" data-overlay-act="bulkClose">
    <div class="modal-box">
      <h3>批量订阅到同一个群</h3>
      <p class="bg-cell-sub">选一个群，把放送表里符合条件的番剧<b>一次性全部订阅过去</b>
        （一部番一条订阅，各自按自己的「周几 + 几点」到点推送）。已经订阅过这个群的会自动跳过。</p>
      <div class="row">
        <label class="bg-field"><span>用哪个平台实例</span>
          <select class="bg-input" data-model="bulk.instance_id" data-after="bulkPickInstance">
            <option value="">请选择</option>
            ${S.targets.instances.map((ins) => `<option value="${esc(ins.id)}"${String(b.instance_id) === String(ins.id) ? ' selected' : ''}>${esc(ins.name)}</option>`).join('')}
          </select></label>
        <label class="bg-field"><span>发到哪个群</span>
          ${groups.length
            ? `<select class="bg-input" data-model="bulk.group" data-after="bulkPickGroup">
                 <option value="">请选择</option>
                 ${groups.map((g) => `<option value="${esc(g.id)}"${String(b.target_id) === g.id ? ' selected' : ''}>${esc(g.name)}</option>`).join('')}
               </select>`
            : `<input class="bg-input" data-model="bulk.target_id" value="${esc(b.target_id)}" placeholder="手填群 ID（session_id）">`}
        </label>
      </div>
      <div class="bg-field bg-field-wide"><span>只订阅周几（一个都不勾 = 不限）</span>
        <div class="bg-chips">
          <button type="button" class="bg-chip${b.weekdays.length === 0 ? ' on' : ''}" data-act="bulkWeekdaysAll">不限</button>
          ${S.weekdays.map((w) => `<button type="button" class="bg-chip${bulkSelectedWeekday(w.key) ? ' on' : ''}" data-act="bulkToggleWeekday" data-arg="${w.key}">${esc(w.label)}</button>`).join('')}
        </div>
      </div>
      <div class="row">
        <label class="bg-field"><span>推送时刻</span>
          <select class="bg-input" data-model="bulk.time_mode">
            <option value="own"${b.time_mode === 'own' ? ' selected' : ''}>每部番各自的首播时刻（推荐）</option>
            <option value="fixed"${b.time_mode === 'fixed' ? ' selected' : ''}>统一时刻</option>
          </select></label>
        ${b.time_mode === 'fixed'
          ? `<label class="bg-field"><span>统一时刻（HH:MM）</span>
               <input class="bg-input" data-model="bulk.time" value="${esc(b.time || DEFAULT_BULK_TIME)}" placeholder="23:00"></label>`
          : ''}
        <label class="bg-field"><span>播出后延后几分钟推</span>
          <input class="bg-input" type="number" min="0" max="1440" data-model="bulk.delay_minutes" value="${esc(b.delay_minutes)}"></label>
      </div>
      ${bulkTotalCount()
        ? `<div class="bg-summary"><b>将新增 ${bulkAddCount()} 条订阅</b>
             ${bulkSkipCount() ? `<span>（跳过 ${bulkSkipCount()} 部已订阅过的）</span>` : ''}
             ${(!b.instance_id || !b.target_id) ? '<span class="muted"> —— 先选好实例和群</span>' : ''}</div>`
        : `<p class="muted">放送表还是空的：先去「每日放送」页点一次<b>「刷新放送表」</b>，回来再批量订阅。</p>`}
      <div class="actions">
        <button data-act="bulkClose">取消</button>
        <button data-act="bulkSubmit" ${b.submitting || !bulkAddCount() ? 'disabled' : ''}>${b.submitting ? '订阅中…' : '一键订阅'}</button>
      </div>
    </div>
  </div>`;
}

function modalConfirm() {
  const c = S.confirm;
  return `<div class="modal" data-overlay-act="confirmClose">
    <div class="modal-box">
      <h3>${esc(c.title)}</h3>
      <p class="bg-cell-sub">${esc(c.tip)}</p>
      <div class="actions">
        <button data-act="confirmClose">取消</button>
        <button class="bg-btn-danger" data-act="confirmDo" ${c.doing ? 'disabled' : ''}>${c.doing ? '删除中…' : `确认删除这 ${c.ids.length} 条`}</button>
      </div>
    </div>
  </div>`;
}

function modalPreview() {
  return `<div class="modal" data-overlay-act="subsPreviewClose">
    <div class="modal-box">
      <h3>推送预览</h3>
      <pre class="bg-preview">${esc(S.preview.text)}</pre>
      <div class="actions"><button data-act="subsPreviewClose">关闭</button></div>
    </div>
  </div>`;
}

function pickTargetType() {
  const f = S.form;
  if (!f) return;
  if (f.target_type === 'private' && !allowPrivate()) f.target_type = 'group';
  if (f.target_type === 'private') { S.picks.group = ''; f.target_id = ''; f.target_name = ''; }
  render();
}

function subsEditNew() { subsEditOpenItem({}, false); }
