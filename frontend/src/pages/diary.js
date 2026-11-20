import {diary, datasets} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc, date, notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {Attribution, AttributionTone, BuildStatus, Basis, State} from '../constants/diary.js';

const canWrite = () => session.user.role !== 'viewer';
const isAdmin = () => session.user.role === 'admin';

function tag(text, tone = 'plain') {
  return '<span class="tag ' + tone + '">' + esc(text) + '</span>';
}

function sourceLine(group) {
  if (!group.sources || !group.sources.length) return tag('待核', 'alert');
  return group.sources.map(s => tag(s.node, s.basis === 'diary' ? 'strong' : s.basis === 'missing' ? 'alert' : 'muted')).join(' ');
}

function wiringText(wiring) {
  if (!wiring || !wiring.edges) return '未登记';
  return wiring.edges.map(e => esc(e.upstream) + ' → ' + esc(e.downstream)).join('；');
}

/* ------------------------------------------------------------------ 接线 */

function wiringPanel(wiring) {
  const latest = wiring.length ? wiring[wiring.length - 1] : null;
  const form = canWrite()
    ? '<form id="wiring-form" class="stack">'
      + '<label>生效日<input type="date" name="effective_from" value="' + new Date().toISOString().slice(0, 10) + '" required></label>'
      + '<label>接线关系（每行一条：上游 → 下游）<textarea name="edges" rows="4" placeholder="卫星上行 → 新闻一频&#10;新闻一频 → 文化二频" required></textarea></label>'
      + '<label>信号源（无播控日记的接线根，逗号分隔）<input name="sources" placeholder="卫星上行"></label>'
      + '<label>备注<input name="note" maxlength="500" placeholder="例如：备用路由切换前的接线"></label>'
      + '<label class="inline"><input type="checkbox" name="allow_future"> 这是预告生效的接线（生效日晚于今天）</label>'
      + '<button type="submit" class="primary">登记接线版本</button></form>'
    : '<p class="hint">观察员只能查看接线版本。</p>';
  return '<section class="panel"><div class="section-title"><h2>接线关系与生效日</h2><span>' + wiring.length + ' 个版本</span></div>'
    + '<p class="hint">停播日按“生效日不晚于当天”的最新一版接线归并；旧版本永久保留，历版日记依据都用当时的接线快照。</p>'
    + (latest
      ? '<div class="wiring-now"><span>当前可用接线（自 ' + esc(latest.effective_from) + '）</span><strong>' + wiringText(latest) + '</strong>'
        + (latest.sources && latest.sources.length ? '<small>信号源：' + esc(latest.sources.join('、')) + '</small>' : '') + '</div>'
      : '<p class="warning">尚未登记任何接线；在此之前所有停播都会标为待核。</p>')
    + '<ol class="wiring-list">' + wiring.slice().reverse().map(w =>
      '<li><span class="mono">' + esc(w.effective_from) + '</span><div><strong>' + wiringText(w) + '</strong>'
      + '<small>' + (w.sources && w.sources.length ? '信号源 ' + esc(w.sources.join('、')) + ' · ' : '') + esc(w.note || '无备注') + ' · ' + esc(w.created_by) + '</small></div></li>'
    ).join('') + '</ol>'
    + form + '</section>';
}

export function bindWiring(refresh) {
  const form = document.querySelector('#wiring-form');
  if (!form) return;
  submit(form, async f => {
    const lines = String(f.get('edges') || '').split('\n').map(l => l.trim()).filter(Boolean);
    const edges = lines.map(line => {
      const parts = line.split(/→|->|=>/).map(s => s.trim()).filter(Boolean);
      if (parts.length !== 2) throw new Error('接线行须写成“上游 → 下游”：' + line);
      return {upstream: parts[0], downstream: parts[1]};
    });
    const sources = String(f.get('sources') || '').split(/[,，、\s]+/).map(s => s.trim()).filter(Boolean);
    await diary.addWiring({
      effective_from: f.get('effective_from'), edges, sources,
      note: f.get('note') || '', allow_future: f.get('allow_future') === 'on',
    });
    notify('接线版本已登记');
    await refresh();
  });
}

/* ------------------------------------------------------------------ 批次 */

function batchPanel(batches, dsList) {
  const bases = batches.filter(b => b.kind === 'base');
  const options = dsList.map(d => '<option value="' + d.id + '">' + esc(d.name) + ' · ' + d.rows + ' 条</option>').join('');
  const parentOptions = bases.map(b => '<option value="' + b.id + '">' + esc(b.label || ('基础批 #' + b.id)) + '</option>').join('');
  const form = canWrite()
    ? '<form id="batch-form" class="stack">'
      + '<label>日记数据批次<select name="dataset_id" required>' + options + '</select></label>'
      + '<div class="parameter-grid"><label>批次类型<select name="kind"><option value="base">基础批次</option><option value="supplement">补报批次</option></select></label>'
      + '<label>所补基础批次<select name="parent_batch_id"><option value="">（补报时必选）</option>' + parentOptions + '</select></label></div>'
      + '<label>批次说明<input name="label" maxlength="200" placeholder="例如：11-20 补报新闻一频"></label>'
      + '<button type="submit" class="primary">登记日记批次</button></form>'
    : '';
  return '<section class="panel"><div class="section-title"><h2>停播日记批次</h2><span>' + batches.length + ' 批</span></div>'
    + '<p class="hint">基础批次承载整份播控日记；补报批次只携带被补的“链路 + 日”，归并时按行覆盖。<a href="#/datasets">去数据接入导入新日记 ↗</a></p>'
    + '<div class="batch-list">' + batches.map(b =>
      '<article class="batch-card"><div><strong>' + esc(b.label || ('批次 #' + b.id)) + '</strong>'
      + tag(b.kind === 'supplement' ? '补报' : '基础', b.kind === 'supplement' ? 'warn' : 'plain')
      + '<p>' + esc(b.dataset_name) + ' · ' + b.days + ' 天 · ' + esc(b.first_day || '—') + ' → ' + esc(b.last_day || '—') + '</p>'
      + '<small>SHA256 ' + esc(b.checksum.slice(0, 20)) + '…' + (b.parent_batch_id ? ' · 补至 #' + b.parent_batch_id : '') + '</small></div>'
      + '<button class="button small" data-build="' + b.id + '">归并此批</button></article>'
    ).join('') + (!batches.length ? '<p class="empty">还没有批次。先到数据接入导入播控日记。</p>' : '') + '</div>'
    + form + '</section>';
}

/* ------------------------------------------------------------------ 版本 */

function versionCard(build, active) {
  return '<article class="version' + (active ? ' active' : '') + '" data-version="' + build.id + '">'
    + '<div class="version-head"><div><span class="vno">v' + build.version_no + '</span>'
    + '<strong>' + esc(build.batch_label || ('批次 #' + build.batch_id)) + '</strong>'
    + tag(BuildStatus[build.status] || build.status, build.status === 'published' ? 'strong' : build.status === 'ready' ? 'warn' : 'plain') + '</div>'
    + '<small>' + date(build.created_at) + ' · 接线水位 #' + build.wiring_watermark + ' · 阈值 ' + build.threshold + ' 分钟</small></div>'
    + '<p class="hint">' + build.groups + ' 组（待核 ' + build.pending_groups + ' · 复用 ' + build.reused_groups + ' · 重算 ' + build.recomputed_groups + '）'
    + (build.superseded_at ? ' · 已被后续版本替代，仅供查证' : '') + '</p>'
    + (build.error ? '<p class="warning">' + esc(build.error) + '</p>' : '')
    + '<div class="version-actions">'
    + '<button class="text-button" data-open="' + build.id + '">查日记</button>'
    + (build.status === 'draft' || build.status === 'failed' || build.status === 'building'
      ? (canWrite() ? '<button class="text-button" data-retry="' + build.id + '">续算</button>' : tag('计算中', 'muted'))
      : '')
    + (build.status === 'ready' && isAdmin()
      ? '<button class="button small primary" data-publish="' + build.id + '">算齐，切换生效</button>' : '')
    + '</div></article>';
}

function groupCard(group, open) {
  const members = group.members || [];
  return '<article class="group ' + AttributionTone[group.attribution] + (open ? ' open' : '') + '" data-group="' + group.id + '">'
    + '<header data-toggle-group="' + group.id + '">'
    + '<div class="group-day">' + esc(group.day) + '</div>'
    + '<div class="group-main"><strong>' + (members.map(m => esc(m.link)).join(' + ') || '待核稿') + '</strong>'
    + '<span>' + tag(Attribution[group.attribution] || group.attribution, AttributionTone[group.attribution]) + ' 疑似源头：' + sourceLine(group) + '</span></div>'
    + '<div class="group-side">' + members.map(m => '<span class="stop-min">' + esc(m.interruption_min) + '′</span>').join('')
    + (group.reused ? tag('沿用生效版', 'muted') : tag('本版重算', 'plain')) + '</div></header>'
    + '<div class="group-body">'
    + (group.pending_reason ? '<p class="warning">待核原因：' + esc(group.pending_reason) + '</p>' : '')
    + (group.note ? '<p class="group-note">' + esc(group.note) + '</p>' : '')
    + '<div class="two-columns"><div><h4>各链路归因</h4><ul class="member-list">'
    + members.map(m =>
      '<li><strong>' + esc(m.link) + '</strong> ' + tag(Attribution[m.attribution] || m.attribution, AttributionTone[m.attribution])
      + '<span>停播 ' + esc(m.interruption_min) + ' 分钟</span>'
      + (m.suspected_sources && m.suspected_sources.length
        ? '<small>依据：' + m.suspected_sources.map(s => esc(s.node) + '（' + Basis[s.basis] + '）').join('、') + '</small>' : '')
      + (m.duration_note ? '<small class="alert">' + esc(m.duration_note) + '</small>' : '')
      + '</li>').join('') + '</ul></div>'
    + '<div><h4>当日接线与日记依据</h4>' + evidenceBlock(group.evidence) + '</div></div>'
    + '</div></article>';
}

function evidenceBlock(evidence) {
  if (!evidence) return '<p class="hint">无依据快照。</p>';
  const rows = Object.keys(evidence.records || {}).sort().map(node => {
    const r = evidence.records[node];
    return '<tr><td>' + esc(node) + '</td><td>' + (r ? esc(r.interruption_min) + ' / ' + esc(r.planned_min) : tag('缺报', 'alert')) + '</td></tr>';
  }).join('');
  return '<div class="evidence">'
    + '<p><span>接线版本</span> #' + esc(evidence.wiring_id ?? '—') + (evidence.wiring_effective_from ? '（自 ' + esc(evidence.wiring_effective_from) + '）' : '') + '</p>'
    + '<p><span>接线</span> ' + (evidence.edges && evidence.edges.length ? evidence.edges.map(e => esc(e.upstream) + ' → ' + esc(e.downstream)).join('；') : '无') + '</p>'
    + '<table><thead><tr><th>节点</th><th>停播 / 计划(分钟)</th></tr></thead><tbody>' + rows + '</tbody></table>'
    + '</div>';
}

function daySection(day) {
  return '<section class="day-block"><div class="day-head"><h3>' + esc(day.day) + '</h3>'
    + '<span>' + day.groups.length + ' 组' + (day.pending ? ' · 待核 ' + day.pending : '') + '</span></div>'
    + day.groups.map(g => groupCard(g, false)).join('') + '</section>';
}

/* ------------------------------------------------------------------ 页面 */

export async function diaryPage() {
  const [wiring, batches, dsList, builds] = await Promise.all([
    diary.wiring(), diary.batches(), datasets.list(), diary.builds(),
  ]);
  workspace.diary = {wiring, batches, builds};
  // 每个基础批次各自有生效版本；账本顶部展示最近一次发布的那个。
  const published = builds.filter(b => b.status === 'published')
    .sort((a, b) => (b.published_at || 0) - (a.published_at || 0));
  const live = published[0];
  const pending = builds.filter(b => b.status === 'ready');
  const alertBox = pending.length
    ? '<div class="alert-box">' + pending.length + ' 个版本已算齐待切换'
      + (isAdmin() ? '：<button class="text-button" data-publish="' + pending[0].id + '">切换最早的待切换版 ↗</button>'
        : '；切换需业务负责人操作。') + '</div>'
    : '';
  return heading('05 / OUTAGE LEDGER', '停播归并与同源核对',
    '同一停播日按当日接线归并：中继截断归因到上游，上游缺报标待核，多源头全列出。补报只重算受影响的组，算齐后一次性切换。',
    '<button class="button" id="refresh-diary">刷新账本 ↻</button>')
    + '<div class="ledger-banner">'
    + (live
      ? '<div><span>当前生效 v' + live.version_no + '</span><strong>' + esc(live.batch_label || ('批次 #' + live.batch_id)) + '</strong>'
        + '<small>' + live.groups + ' 组 · 待核 ' + live.pending_groups + ' · 发布于 ' + date(live.published_at) + '</small></div>'
      : '<div><span>尚无生效版本</span><strong>先归并一个基础批次，再切换生效</strong></div>')
    + alertBox
    + '</div>'
    + '<div class="ledger-grid">'
    + '<div>' + wiringPanel(wiring) + batchPanel(batches, dsList) + '</div>'
    + '<div><section class="panel"><div class="section-title"><h2>归并版本</h2><span>重试不新增版本</span></div>'
    + '<div class="version-list" id="version-list">'
    + builds.map(b => versionCard(b, live && b.id === live.id)).join('')
    + (!builds.length ? '<p class="empty">还没有归并版本。选择上面的批次点“归并此批”。</p>' : '')
    + '</div></section>'
    + '<section class="panel"><div class="section-title"><h2>当日停播日记</h2><span id="ledger-sub">选择版本查看</span></div>'
    + '<div id="ledger-days"><p class="empty">点开一个版本，按日查看归并结果、疑似源头、待核原因和历版依据。</p></div>'
    + '</section></div></div>';
}

export function bindDiary(refresh) {
  const rerender = () => refresh();
  document.querySelector('#refresh-diary').onclick = rerender;
  bindWiring(refresh);

  submit(document.querySelector('#batch-form'), async f => {
    const kind = f.get('kind');
    const parent = f.get('parent_batch_id');
    if (kind === 'supplement' && !parent) throw new Error('补报批次须选择所补的基础批次');
    await diary.addBatch({
      dataset_id: Number(f.get('dataset_id')), kind,
      parent_batch_id: parent ? Number(parent) : null, label: f.get('label') || '',
    });
    notify('日记批次已登记');
    await rerender();
  });

  document.querySelectorAll('[data-build]').forEach(b => b.onclick = async () => {
    try {
      const res = await diary.createBuild({batch_id: Number(b.dataset.build)});
      notify(res.created ? '已开始归并 v' + res.build.version_no : '已续算原版本 v' + res.build.version_no);
      await rerender();
    } catch (e) { notify(e.message, true); }
  });
  bindBuildActions(rerender);
  const first = document.querySelector('[data-version]');
  if (first) openBuild(Number(first.dataset.version));
}

function bindBuildActions(rerender) {
  document.querySelectorAll('[data-open]').forEach(b => b.onclick = () => openBuild(Number(b.dataset.open)));
  document.querySelectorAll('[data-retry]').forEach(b => b.onclick = async () => {
    try { await diary.retry(Number(b.dataset.retry)); notify('已续算，稍后刷新'); await rerender(); }
    catch (e) { notify(e.message, true); }
  });
  document.querySelectorAll('[data-publish]').forEach(b => b.onclick = async () => {
    if (!confirm('确认该版本已算齐，切换为生效版本？旧版会保留可查。')) return;
    try { await diary.publish(Number(b.dataset.publish)); notify('已切换生效版本'); await rerender(); }
    catch (e) { notify(e.message, true); }
  });
}

async function openBuild(id) {
  const box = document.querySelector('#ledger-days');
  const sub = document.querySelector('#ledger-sub');
  if (!box) return;
  box.innerHTML = '<p class="empty">正在读取该版日记…</p>';
  try {
    const data = await diary.build(id);
    const b = data.build;
    if (sub) sub.textContent = 'v' + b.version_no + ' · ' + b.groups + ' 组 · ' + BuildStatus[b.status];
    if (b.status === 'draft' || b.status === 'building') {
      box.innerHTML = '<p class="warning">该版本尚未算齐' + (b.error ? '：' + esc(b.error) : '，稍后刷新或点“续算”。') + '</p>';
      return;
    }
    box.innerHTML = data.days.length
      ? data.days.map(daySection).join('')
      : '<p class="empty">该版没有停播日。</p>';
    box.querySelectorAll('[data-toggle-group]').forEach(h => h.onclick = () => {
      h.parentElement.classList.toggle('open');
    });
  } catch (e) {
    box.innerHTML = '<p class="warning">' + esc(e.message) + '</p>';
  }
}
