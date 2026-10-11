/* 子页签：订阅数据（群订阅）—— 哪些群订了番剧推送、谁订的、订了几部，逐群管理
 * ------------------------------------------------------------------
 * 一行 = 一个群（群里发「订阅番剧」命令登记下来的，面板也能手工补一条）。
 * 「这个群订了哪几部番」不是另一份数据 —— 它就是 store 里的规则，按
 * platform_id + target_id 归到一起，所以：
 *   · 逐部番的**新增**走 rule/save（复用订阅编辑那份表单校验）
 *   · 逐部番的**删除**走 rule/delete
 *   · 「一键订本季全部」走 subs/anime/all（后端 bulk.py）
 *   · 只有「群」这一层（启停 / 删除 / 群名订阅者）才走 subs/* 接口
 *
 * 几个刻意的取舍：
 * - **删除是二次确认浮层**（不是 markConfirm）：删一个群会连带删掉它几十条番剧
 *   订阅，浮层里能把「将一并删除 N 条」说清楚，两段式按钮说不了。
 * - 「停用」只是不再推给该群，**不删规则** —— 解禁时后端会把该群的规则重排，
 *   否则积压的那几十条会一次性炸出来。
 * - 番剧浮层里选放送表条目的下标存在 `_animePicks`，按钮只带一个短 key
 *   （和日历页的 `_calMap` 同一个套路，避免把整个对象塞进 data-arg）。
 */

/* 渲染时铺的候选表（浮层里的下拉 + 搜索结果卡片都按下标/key 引用它） */
const _animePicks = [];
const _animeSearch = {};
let _animeSearchIdx = 0;

/* ---------------- 取数 ---------------- */

async function subdataLoad() {
  S.gsubs.loading = true; render();
  const j = await GET('subs');
  S.gsubs.loading = false;
  if (!j.ok) {
    notify(j.message || '订阅数据加载失败', 'err');
    S.gsubs.list = [];
    render();
    return;
  }
  S.gsubs.list = j.subs || [];
  S.gsubs.counts = j.counts || {};
  if (j.settings) S.settings = Object.assign(S.settings, j.settings);
  render();
}

/* 权限档位（群主+管理员 / 仅群主）：勾选框一变就存 */
async function subdataPermChanged() {
  const j = await POST('settings', { owner_only: !!S.settings.owner_only });
  if (!j.ok) { notify(j.message || '保存失败', 'err'); await loadState(); render(); return; }
  notify(S.settings.owner_only ? '已改为：仅群主可以「订阅番剧」' : '已改为：群主 + 群管理员都可以「订阅番剧」', 'ok');
  if (j.settings) S.settings = Object.assign(S.settings, j.settings);
  render();
}

/* ---------------- 定位 ---------------- */

/* 「平台实例 + 群」拼成短 key 给 data-arg 用（data-arg 只吃字符串） */
function subKey(s) { return String(s.platform_id) + '|' + String(s.group_id); }

function subByKey(key) {
  const parts = String(key).split('|');
  return S.gsubs.list.find(
    (s) => String(s.platform_id) === parts[0] && String(s.group_id) === parts[1],
  ) || null;
}

function subById(pid, gid) {
  return S.gsubs.list.find(
    (s) => String(s.platform_id) === String(pid) && String(s.group_id) === String(gid),
  ) || null;
}

/* 当前番剧浮层对应的群订阅（**每次现取**：列表一刷新，旧的快照就过期了） */
function animeSub() { return subById(S.anime.pid, S.anime.gid); }

function animeRules() {
  const a = S.anime;
  return S.rules.filter(
    (r) => String(r.platform_id) === String(a.pid) && String(r.target_id) === String(a.gid),
  );
}

/* ---------------- 群订阅操作 ---------------- */

async function subToggle(key) {
  const s = subByKey(key);
  if (!s) return;
  const j = await POST('subs/toggle', {
    platform_id: s.platform_id, group_id: s.group_id, enabled: !s.enabled,
  });
  notify(j.message || (j.ok ? '已切换' : '切换失败'), j.ok ? 'ok' : 'err');
  if (j.ok) { subdataLoad(); loadState(); }
}

function subDelete(key) {
  const s = subByKey(key);
  if (!s) return;
  const n = Number(s.anime_total || 0);
  confirmOpen(
    `删除群订阅：${s.group_name || s.group_id}`,
    [key],
    `将同时删除该群的 ${n} 条番剧订阅，删除后无法恢复。只想暂时不收，用「停用」就好。`,
    { kind: 'group', pid: s.platform_id, gid: s.group_id },
  );
}

/* ---------------- 手工新增 / 编辑群订阅 ---------------- */

async function subAddOpen(key) {
  if (!S.targets.instances.length) await subsTargets();
  const s = key ? subByKey(key) : null;
  S.subAdd = {
    edit: !!s,
    platform_id: s ? String(s.platform_id) : '',
    group_id: s ? String(s.group_id) : '',
    group_name: s ? String(s.group_name || '') : '',
    subscriber_name: s ? String(s.subscriber_name || '') : '',
    pick: '',
  };
  render();
}

function subAddCancel() { S.subAdd = null; render(); }

function subAddPickInstance() {
  const f = S.subAdd;
  if (!f || f.edit) return;
  f.pick = '';
  f.group_id = '';
  f.group_name = '';
  render();
}

function subAddPickGroup() {
  const f = S.subAdd;
  if (!f || f.edit) return;
  const hit = groupsOf(f.platform_id).find((g) => g.id === String(f.pick));
  if (hit) {
    f.group_id = hit.id;
    if (!String(f.group_name || '').trim()) f.group_name = hit.name;
  }
  render();
}

async function subAddSave() {
  const f = S.subAdd;
  if (!f) return;
  if (!String(f.platform_id || '').trim()) { notify('请选择平台实例', 'err'); return; }
  if (!String(f.group_id || '').trim()) { notify('请选择 / 填写群 ID', 'err'); return; }
  const j = await POST('subs/save', {
    platform_id: f.platform_id,
    group_id: f.group_id,
    group_name: f.group_name,
    subscriber_name: f.subscriber_name,
  });
  if (!j.ok) { notify(j.message || '保存失败', 'err'); return; }
  notify(j.message || '已保存', 'ok');
  S.subAdd = null;
  subdataLoad();
  loadState();
}

/* ---------------- 番剧浮层 ---------------- */

async function animeOpen(key) {
  const s = subByKey(key);
  if (!s) return;
  S.anime = {
    open: true, pid: s.platform_id, gid: s.group_id,
    item: '', q: '', results: [], searching: false, busy: false,
  };
  render();
  // 两个数据源都是**只读缓存**（放送表、规则列表），弹浮层不该顺带联网拉新数据
  const jobs = [];
  if (!(S.calendar.days || []).length) jobs.push(calLoad(false));
  if (!S.rules.length) jobs.push(subsLoad());
  if (jobs.length) { await Promise.all(jobs); render(); }
}

function animeClose() {
  S.anime.open = false;
  S.anime.results = [];
  render();
}

function animeClearResults() { S.anime.results = []; render(); }

/* 放送表里「还没订给这个群」的候选（按条目去重，按名字排好） */
function animeCandidates() {
  const have = new Set(animeRules().map(_animeKey));
  const seen = new Set();
  const out = [];
  for (const d of (S.calendar.days || [])) {
    for (const it of (d.items || [])) {
      const k = _animeKey(it);
      if (have.has(k) || seen.has(k)) continue;
      seen.add(k);
      out.push(Object.assign({}, it, {
        weekday: Number(it.weekday || d.weekday) || Number(d.weekday),
      }));
    }
  }
  out.sort((a, b) => String(a.title || '').localeCompare(String(b.title || ''), 'zh'));
  return out;
}

function _animeKey(it) {
  const sid = String((it && it.subject_id) || '');
  return sid ? 'id:' + sid : 't:' + String((it && it.title) || '').trim();
}

/* 把一条放送表 / 搜索结果条目订给当前群（周几、时刻都按它自己的来） */
async function animeSubscribeItem(it) {
  const s = animeSub();
  const a = S.anime;
  if (!s || !it) return;
  a.busy = true; render();
  const payload = Object.assign({}, it, {
    // 目标改用这个群（条目里没有这些字段，也不该有）
    platform_id: s.platform_id,
    platform_name: s.platform_id,
    target_type: 'group',
    target_id: s.group_id,
    target_name: s.group_name || s.group_id,
    // 页面上的写法与「我的订阅」保持一致：总集数 / 周期 / 周几 / 启用
    total_episodes: Number(it.total || 0),
    interval_days: Number(it.interval || 7) || 7,
    weekday: Number(it.weekday || 1) || 1,
    delay_minutes: Number(it.delay_minutes || 10),
    template: '',
    enabled: true,
  });
  const j = await POST('rule/save', payload);
  a.busy = false;
  notify(j.message || (j.ok ? '已订阅' : '订阅失败'), j.ok ? 'ok' : 'err');
  if (j.ok) {
    a.item = '';
    await subsLoad();
    await subdataLoad();
  }
  render();
}

function animeAdd() {
  const pick = _animePicks[Number(S.anime.item)];
  if (!pick) { notify('请先选一部番剧', 'err'); return; }
  animeSubscribeItem(pick);
}

function animeAddItem(key) {
  const it = _animeSearch[key];
  if (it) animeSubscribeItem(it);
}

async function animeAddAll() {
  const s = animeSub();
  const a = S.anime;
  if (!s) return;
  a.busy = true; render();
  const j = await POST('subs/anime/all', { platform_id: s.platform_id, target_id: s.group_id });
  a.busy = false;
  notify(j.message || (j.ok ? '已订阅' : '订阅失败'), j.ok ? 'ok' : 'err');
  if (j.ok) { await subsLoad(); await subdataLoad(); }
  render();
}

async function animeToggle(id) {
  const r = ruleById(id);
  if (!r) return;
  const j = await POST('rule/toggle', { id: r.id, enabled: !r.enabled });
  notify(j.message || (j.ok ? '已切换' : '切换失败'), j.ok ? 'ok' : 'err');
  if (j.ok) { await subsLoad(); await subdataLoad(); }
}

async function animeRemove(id, el) {
  if (!markConfirm(el)) return;      // 单条番剧：两段式确认够用（不会牵连别的东西）
  const j = await POST('rule/delete', { id });
  notify(j.message || (j.ok ? '已删除' : '删除失败'), j.ok ? 'ok' : 'err');
  if (j.ok) { await subsLoad(); await subdataLoad(); }
}

async function animeSearch() {
  const a = S.anime;
  const q = String(a.q || '').trim();
  if (!q) { notify('请输入番剧名', 'err'); return; }
  a.searching = true; render();
  const j = await GET('search', { q });
  a.searching = false;
  if (!j.ok) { notify(j.message || '搜索失败', 'err'); render(); return; }
  a.results = j.items || [];
  if (!a.results.length) notify('没搜到，换个名字试试（可试试日文原名）', 'err');
  render();
}

/* ---------------- 渲染 ---------------- */

function renderSubdata() {
  const g = S.gsubs;
  const c = g.counts || {};
  let h = `<div class="card">`;

  h += `<div class="toolbar">
    <button data-act="subAddOpen">+ 手工添加订阅群</button>
    <button class="ghost" data-act="subdataLoad" ${g.loading ? 'disabled' : ''}>${g.loading ? '刷新中…' : '刷新'}</button>
    <label class="check" title="打开后「订阅番剧」命令只有群主（和 AstrBot 管理员）能用">
      <input type="checkbox" data-model="settings.owner_only" data-after="subdataPermChanged"${S.settings.owner_only ? ' checked' : ''}>
      <span class="muted">「订阅番剧」仅群主可用</span>
    </label>
    <span class="muted nowrap">共 ${Number(c.groups || 0)} 个群（启用 ${Number(c.online || 0)} / 停用 ${Number(c.offline || 0)}）
      · 番剧订阅 ${Number(c.anime || 0)} 条</span>
  </div>`;

  if (g.list.length) {
    h += `<table class="bg-table"><thead><tr>
      <th style="width:16%">群 ID</th>
      <th style="width:18%">群名</th>
      <th style="width:18%">订阅者</th>
      <th style="width:10%">番剧</th>
      <th style="width:10%">状态</th>
      <th>操作</th>
    </tr></thead><tbody>`;
    for (const s of g.list) {
      const key = esc(subKey(s));
      h += `<tr>
        <td>
          <div class="bg-cell-title">${esc(s.group_id)}</div>
          <div class="bg-cell-sub">${esc(s.platform_type || s.platform_id || '—')}</div>
        </td>
        <td>
          <div class="bg-cell-title" title="${esc(s.group_name || '')}">${esc(s.group_name || '（未命名）')}</div>
          ${s.group_name ? '' : '<div class="bg-cell-sub">QQ 官方机器人的群名可在「编辑」里补填</div>'}
        </td>
        <td>
          <div>${esc(s.subscriber_name || '—')}</div>
          <div class="bg-cell-sub">${esc(s.subscriber_id || '—')}</div>
        </td>
        <td>${Number(s.anime_enabled || 0)} / ${Number(s.anime_total || 0)}</td>
        <td><span class="badge ${s.enabled ? 'online' : 'offline'}">${s.enabled ? '启用' : '停用'}</span></td>
        <td>
          <button class="bg-btn-xs" data-act="subToggle" data-arg="${key}">${s.enabled ? '停用' : '启用'}</button>
          <button class="bg-btn-xs" data-act="animeOpen" data-arg="${key}">番剧</button>
          <button class="bg-btn-xs" data-act="subEditOpen" data-arg="${key}">编辑</button>
          <button class="bg-btn-xs bg-btn-danger" data-act="subDelete" data-arg="${key}">删除</button>
        </td>
      </tr>`;
    }
    h += `</tbody></table>`;
  } else {
    h += `<p class="muted">还没有任何群订阅。到群里发一条「<b>订阅番剧</b>」（群主 / 群管理员）
      就能把本季正在播的番剧全部订到那个群；也可以点上面的「手工添加订阅群」自己加一个。</p>`;
  }

  h += `<div class="guide">
    <p class="guide-title">这个页面管的是什么</p>
    <ul class="guide-list">
      <li><b>群订阅</b>是「哪个群收番剧推送」这一层：群里发「订阅番剧」就登记一条，
          记下群 ID、群名、是谁订的</li>
      <li><b>番剧</b>那一列是「该群订了几部番（启用 / 总数）」—— 点「番剧」进去可以
          <b>单加 / 单删某部</b>，也可以「一键订本季全部」</li>
      <li><b>停用</b>：该群不再收到任何番剧更新，但订阅都留着，随时可以再启用
          （启用时会把该群的订阅重排到下一次播出，不会一次性把积压的全发出来）</li>
      <li><b>删除</b>：这条群订阅 + 该群的<b>全部番剧订阅</b>一起删掉，不可恢复</li>
      <li><b>权限</b>：默认群主和群管理员都能发「订阅番剧」；勾上「仅群主可用」就只有群主
          （AstrBot 管理员始终放行，免得角色拿不到时没人能改回来）</li>
      <li>「订阅番剧」= 登记这个群 + 把<b>本季正在播的番剧全部</b>订过来（各按各自的
          播出周几 / 时刻推，不是统一一个时间）。退订：「取消订阅番剧」</li>
    </ul>
  </div></div>`;

  return h;
}

function animeCard(it) {
  const key = 'a' + (++_animeSearchIdx);
  _animeSearch[key] = it;
  const poster = it.image
    ? `<img class="bg-poster-img" src="${esc(it.image)}" referrerpolicy="no-referrer" alt="${esc(it.title || '')}" loading="lazy">`
    : `<div class="bg-poster-ph">${esc(it.title || it.native || '')}</div>`;
  const week = wdChar(it.weekday)
    ? `<div class="bg-tag bg-tag-week bg-w${Number(it.weekday)}" title="每周${wdChar(it.weekday)}播出">
         <b>${wdChar(it.weekday)}</b>${it.time ? `<span>${esc(it.time)}</span>` : ''}
       </div>`
    : '';
  return `<div class="bg-card">
    <div class="bg-poster">
      ${poster}${week}
      <button class="bg-poster-sub" data-act="animeAddItem" data-arg="${key}">订阅</button>
    </div>
    <div class="bg-card-title" title="${esc(it.native || it.title || '')}">${esc(it.title || it.native || '')}</div>
  </div>`;
}

/* ---------------- 浮层 ---------------- */

/* 某个群的番剧订阅：列表（可停用 / 删单条）+ 从放送表加一部 + 搜索 + 一键订全部 */
function modalAnime() {
  const a = S.anime;
  const s = animeSub();
  if (!a.open || !s) return '';

  const rules = animeRules();
  const cands = animeCandidates();
  _animePicks.length = 0;
  for (const it of cands) _animePicks.push(it);
  // 搜索结果卡片的 key→条目表也跟着这次渲染重建（和 _animePicks 同一个套路）
  _animeSearchIdx = 0;
  for (const k in _animeSearch) delete _animeSearch[k];

  let body;
  if (rules.length) {
    body = `<table class="bg-table"><thead><tr>
      <th>番剧</th><th style="width:24%">播出</th><th style="width:12%">状态</th><th style="width:18%"></th>
    </tr></thead><tbody>`
      + rules.map((r) => `<tr>
          <td>
            <div class="bg-cell-title" title="${esc(r.native || '')}">${esc(r.title || '（未命名）')}</div>
            ${r.air_date ? `<div class="bg-cell-sub">首播 ${esc(r.air_date)}${r.total_episodes ? ` · 全 ${Number(r.total_episodes)} 集` : ''}</div>` : ''}
          </td>
          <td>${esc(subsWhenText(r))}</td>
          <td><span class="badge ${r.enabled ? 'online' : 'offline'}">${r.enabled ? '启用' : '停用'}</span></td>
          <td>
            <button class="bg-btn-xs" data-act="animeToggle" data-arg="${esc(String(r.id))}">${r.enabled ? '停用' : '启用'}</button>
            <button class="bg-btn-xs bg-btn-danger" data-act="animeRemove" data-arg="${esc(String(r.id))}">删除</button>
          </td>
        </tr>`).join('')
      + `</tbody></table>`;
  } else {
    body = `<p class="muted">这个群还没有订阅任何番剧。点下面的「一键订本季全部」，
      或者从放送表里挑一部加进来。</p>`;
  }

  const options = cands.map((it, i) =>
    `<option value="${i}"${String(a.item) === String(i) ? ' selected' : ''}>${esc(it.title || it.native || '')}（${esc(wdText(it.weekday))} ${esc(it.time || '')}）</option>`,
  ).join('');

  return `<div class="modal" data-overlay-act="animeClose">
    <div class="modal-box modal-wide">
      <h3>${esc(s.group_name || s.group_id)} · 番剧订阅</h3>
      <p class="bg-cell-sub">
        群 ID ${esc(s.group_id)} · 平台 ${esc(s.platform_type || s.platform_id || '—')}
        · 订阅者 ${esc(s.subscriber_name || '—')}${s.subscriber_id ? `（${esc(s.subscriber_id)}）` : ''}
        · 该群状态 <b>${s.enabled ? '启用' : '停用'}</b>
      </p>
      ${body}
      <div class="row" style="margin-top:10px">
        <label class="bg-field grow"><span>从放送表加一部（按它自己的播出时刻推）</span>
          <select class="bg-input" data-model="anime.item">
            <option value="">请选择</option>
            ${options}
          </select></label>
        <label class="bg-field" style="flex:0 0 auto"><span>&nbsp;</span>
          <button data-act="animeAdd" ${a.busy || !cands.length ? 'disabled' : ''}>订阅这一部</button></label>
      </div>
      ${cands.length ? '' : '<p class="muted">放送表里暂时没有可加的番：要么还没拉过放送表（去「每日放送」点一次「刷新放送表」），要么本季的番都已在上面了。也可以用下面的搜索。</p>'}
      <div class="row">
        <input type="text" class="grow" data-model="anime.q" data-enter="animeSearch" value="${esc(a.q)}"
               placeholder="放送表里没有？按名字搜（可试日文原名）">
        <button data-act="animeSearch" ${a.searching ? 'disabled' : ''}>${a.searching ? '搜索中…' : '搜索'}</button>
        <button class="ghost" data-act="animeAddAll" ${a.busy ? 'disabled' : ''} title="把放送表里本季的番剧全部订给这个群（已订过的自动跳过）">一键订本季全部</button>
      </div>
      ${a.results.length ? `<div class="bg-panel">
        <div class="bg-panel-head"><b>搜索结果</b>
          <button class="bg-btn-xs" data-act="animeClearResults">收起</button></div>
        <div class="bg-grid">${a.results.map(animeCard).join('')}</div>
      </div>` : ''}
      <div class="actions"><button data-act="animeClose">关闭</button></div>
    </div>
  </div>`;
}

/* 手工新增 / 编辑一条群订阅 */
function modalSubAdd() {
  const f = S.subAdd;
  const groups = f.edit ? [] : groupsOf(f.platform_id);
  let groupField;
  if (f.edit) {
    groupField = `<label class="bg-field"><span>群 ID（不可改）</span>
        <input class="bg-input" value="${esc(f.group_id)}" disabled></label>`;
  } else if (groups.length) {
    groupField = `<label class="bg-field"><span>群</span>
        <select class="bg-input" data-model="subAdd.pick" data-after="subAddPickGroup">
          <option value="">请选择</option>
          ${groups.map((g) => `<option value="${esc(g.id)}"${String(f.group_id) === g.id ? ' selected' : ''}>${esc(g.name)}</option>`).join('')}
        </select></label>`;
  } else {
    groupField = `<label class="bg-field"><span>群 ID（群消息事件里的 session_id，OneBot 就是 QQ 群号）</span>
        <input class="bg-input" data-model="subAdd.group_id" value="${esc(f.group_id)}" placeholder="如 123456789"></label>`;
  }
  return `<div class="modal" data-overlay-act="subAddCancel">
    <div class="modal-box">
      <h3>${f.edit ? '编辑群订阅' : '手工添加订阅群'}</h3>
      <p class="bg-cell-sub">${f.edit
        ? '只能改群名与订阅者名（群 ID 和平台实例定死了，改了就是另一条订阅）。'
        : '加好之后点这一行的「番剧」→「一键订本季全部」，就能把本季番剧都订给它。'}</p>
      <div class="row">
        <label class="bg-field"><span>平台实例</span>
          <select class="bg-input" data-model="subAdd.platform_id" data-after="subAddPickInstance"${f.edit ? ' disabled' : ''}>
            <option value="">请选择</option>
            ${S.targets.instances.map((ins) => `<option value="${esc(ins.id)}"${String(f.platform_id) === String(ins.id) ? ' selected' : ''}>${esc(ins.name)}（${ins.protocol === 'onebot' ? 'OneBot' : (ins.protocol === 'qq_official' ? '官方' : ins.protocol)}）</option>`).join('')}
          </select></label>
        ${groupField}
      </div>
      <div class="row">
        <label class="bg-field"><span>群名（QQ 官方机器人拿不到，可手填）</span>
          <input class="bg-input" data-model="subAdd.group_name" value="${esc(f.group_name)}" placeholder="留空就显示群 ID"></label>
        <label class="bg-field"><span>订阅者名（选填）</span>
          <input class="bg-input" data-model="subAdd.subscriber_name" value="${esc(f.subscriber_name)}" placeholder="谁订的"></label>
      </div>
      ${S.targets.instances.length ? '' : '<p class="muted">没有检测到平台实例：请先在 AstrBot「消息平台」里配置并启用至少一个平台适配器。</p>'}
      <div class="actions">
        <button data-act="subAddCancel">取消</button>
        <button data-act="subAddSave">保存</button>
      </div>
    </div>
  </div>`;
}
