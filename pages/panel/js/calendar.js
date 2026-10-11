/* 子页签：每日放送（原 frag/calendar）—— 本周每天播什么，看中哪部点「订阅」
 * ------------------------------------------------------------------
 * ★ 缓存策略：放送表缓存 24 小时有效，进页面只是读盘，**一天最多真拉一次**；
 *   点「刷新」也要过最小间隔（6 小时），被限住时后端回 `throttled` ——
 *   **这不是出错**，别当 error 弹红字，按普通提示处理。
 * ★ 数据源下拉切换 = 立即保存 + 强制刷新（日历缓存只有一份，不刷就会继续显示旧源的数据）。
 * ★ 卡片上的「已订阅」判定直接看共享状态 S.rules。
 */

/* 渲染时把每个条目的对象存进这张表，按钮只带一个短 key —— 免得把整个对象塞进 data-arg */
let _calIdx = 0;
const _calMap = {};

function calUpdated() {
  const c = S.calendar;
  if (!c.fetched_at) return '还没拉过';
  const d = new Date(Number(c.fetched_at) * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}${c.cached ? '（缓存）' : ''}`;
}

async function calLoad(force) {
  const c = S.calendar;
  c.loading = true; render();
  const j = await GET('calendar', force ? { refresh: '1' } : {});
  c.loading = false;
  if (!j.ok) {
    // 上游连不上：把原因留在页面上，别只飘一个 toast
    c.error = j.message || '每日放送加载失败';
    c.source = j.source || c.source;
    notify(c.error, 'err');
    render();
    return;
  }
  c.days = j.days || [];
  c.cached = !!j.cached;
  c.fetched_at = Number(j.fetched_at || 0);
  c.source = j.source || '';
  if (j.throttled) {
    // 不是出错 —— 「距离上次太近所以先用缓存」说清楚就行，别弹红字吓人
    c.error = '';
    notify(j.message || '放送表用的是缓存', 'ok');
  } else {
    c.error = j.message || '';      // 「刷新失败先用缓存」这类才是真要提示的
  }
  render();
}

function calRefresh() { calLoad(true); }

async function calSearch() {
  const c = S.calendar;
  const q = String(c.q || '').trim();
  if (!q) { notify('请输入番剧名', 'err'); return; }
  c.searching = true; render();
  const j = await GET('search', { q });
  c.searching = false;
  if (!j.ok) { notify(j.message || '搜索失败', 'err'); render(); return; }
  c.results = j.items || [];
  if (!c.results.length) notify('没搜到，换个名字试试（可试试日文原名）', 'err');
  render();
}

/* 数据源下拉：立即保存 + 强制刷新 */
async function calProviderChanged() {
  const j = await POST('settings', { provider: S.settings.provider, enabled: !!S.settings.enabled });
  if (!j.ok) { notify(j.message || '保存失败', 'err'); return; }
  notify('已切换数据源，正在刷新放送表…', 'ok');
  calLoad(true);
  loadState();
}

function calToggleGuide() { S.calendar.guide = !S.calendar.guide; render(); }

function calSubscribe(key) {
  const item = _calMap[key];
  if (item) subsEditOpenItem(item);
}

function calIsSub(id) {
  return S.rules.some((r) => String(r.subject_id) === String(id));
}
function calSubCount(id) {
  return S.rules.filter((r) => String(r.subject_id) === String(id)).length;
}

/* ---------------- 渲染 ---------------- */

function calCard(item, withSub) {
  const key = 'c' + (++_calIdx);
  _calMap[key] = item;
  const done = calIsSub(item.subject_id);
  const cnt = calSubCount(item.subject_id);
  const poster = item.image
    ? `<img class="bg-poster-img" src="${esc(item.image)}" referrerpolicy="no-referrer" alt="${esc(item.title)}" loading="lazy">`
    : `<div class="bg-poster-ph">${esc(item.title)}</div>`;
  const week = wdChar(item.weekday)
    ? `<div class="bg-tag bg-tag-week bg-w${Number(item.weekday)}" title="每周${wdChar(item.weekday)}播出">
         <b>${wdChar(item.weekday)}</b>${item.time ? `<span>${esc(item.time)}</span>` : ''}
       </div>`
    : '';
  const info = `<div class="bg-tag bg-tag-info">
      ${item.score ? `<span>${Number(item.score).toFixed(1)}分</span>` : ''}
      ${item.latest ? `<span>${esc(item.latest)}</span>` : ''}
    </div>`;
  const action = withSub && !done
    ? `<button class="bg-poster-sub" data-act="calSubscribe" data-arg="${key}">订阅</button>`
    : (done ? `<span class="bg-poster-done">已订阅${cnt > 1 ? '×' + cnt : ''}</span>` : '');
  return `<div class="bg-card">
    <div class="bg-poster">
      ${poster}
      ${item.air_date ? `<div class="bg-tag bg-tag-date">${esc(item.air_date)}</div>` : ''}
      ${week}
      ${info}
      ${action}
    </div>
    <div class="bg-card-title" title="${esc(item.native || item.title)}">${esc(item.title)}</div>
  </div>`;
}

function renderCalendar() {
  _calIdx = 0;
  for (const k in _calMap) delete _calMap[k];
  const c = S.calendar;
  let h = `<div class="card">`;

  // 工具栏：搜索 + 刷新 + 数据源 + 更新时间 + 说明
  h += `<div class="toolbar">
    <input type="text" class="grow" data-model="calendar.q" data-enter="calSearch" value="${esc(c.q)}"
           placeholder="日历里没找到？按名字搜（可试日文原名）">
    <button data-act="calSearch" ${c.searching ? 'disabled' : ''}>${c.searching ? '搜索中…' : '搜索'}</button>
    <button data-act="calRefresh" ${c.loading ? 'disabled' : ''}>${c.loading ? '刷新中…' : '刷新放送表'}</button>
    <select class="prov" data-model="settings.provider" data-after="calProviderChanged"
            title="切换数据源后会自动刷新放送表">
      ${S.providers.map((p) => `<option value="${esc(p.key)}"${S.settings.provider === p.key ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}
    </select>
    <span class="muted nowrap">更新于 ${esc(calUpdated())}（${esc(c.ttl_hint || '')}）</span>
    <button class="ghost sm" data-act="calToggleGuide">${c.guide ? '收起说明' : '说明'}</button>
  </div>`;

  // 说明（默认收起）
  if (c.guide) {
    h += `<div class="guide">
      <p class="guide-title">怎么用</p>
      <ul class="guide-list">
        <li>下面按周一~周日分好了组，找到想追的那部点<b>「订阅」</b></li>
        <li>订阅里已经带好<b>每周几 + 几点</b>（数据源给的，可改），再选「用哪个平台发到哪个群」，
            到点就往群里发「《XX》第 N 集更新了」</li>
        <li><b>第几集</b>：这部番在 B站有引进就查 B站时间表（最准，能看出停播/连播）；
            否则按首播日 + 周期推算（双周更也认）</li>
        <li><b>想把整季新番都推到同一个群</b>：去<b>「我的订阅」</b>页点<b>「批量订阅到同一个群」</b>，
            选一次群就行，几十部番会各自按自己的播出时间生成一条订阅（已经订过的自动跳过）</li>
        <li><b>错过了不补推</b>：本周那个时刻已经过去就会排到下一周，不会一保存就炸出一条消息</li>
        <li>没有配到封面的卡片会显示灰底文字，不影响订阅</li>
      </ul>
      <p class="guide-title">数据源</p>
      <ul class="guide-list">
        <li><b>bangumi-data</b>：经 jsDelivr 拿 GitHub 上的日本新番库，一个月 70+ 部，
            带首播时刻与周更周期（含双周更），封面与总集数另从 AniList 补</li>
        <li><b>B站番剧时间表</b>：只含 B站引进的（不全），但集数最准，能看出停播/连播</li>
        <li><b>Bangumi.tv</b>：条目最全，但国内网络经常连不上</li>
        <li>当前数据源：${esc(c.source || '—')}</li>
      </ul>
    </div>`;
  }

  if (c.error) h += `<div class="warn">${esc(c.error)}</div>`;

  // 搜索结果
  if (c.results.length) {
    h += `<div class="bg-panel">
      <div class="bg-panel-head"><b>搜索结果</b>
        <button class="bg-btn-xs" data-act="calClearResults">收起</button></div>
      <div class="bg-grid">${c.results.map((it) => calCard(it, true)).join('')}</div>
    </div>`;
  }

  if (!c.days.length && !c.loading) {
    h += `<p class="muted">${c.error
      ? '暂时拿不到放送表（原因见上方红框）。'
      : '还没有放送数据，点上面的「刷新放送表」拉一次。'}
      只想推送的话，也可以去「我的订阅」里手动添加：填番剧名 + 首播日 + 每周几几点就行。</p>`;
  }

  for (const d of c.days) {
    h += `<div class="bg-day">
      <div class="bg-day-head"><b>${esc(d.label)}</b><span class="muted">${(d.items || []).length} 部</span></div>
      ${(d.items || []).length
        ? `<div class="bg-grid">${d.items.map((it) => calCard(it, true)).join('')}</div>`
        : '<p class="muted">今天没有在播的番剧</p>'}
    </div>`;
  }

  h += `</div>`;
  return h;
}

function calClearResults() { S.calendar.results = []; render(); }
