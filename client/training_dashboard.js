const $ = id => document.getElementById(id);
let pointerDown = false;
document.addEventListener('pointerdown', () => { pointerDown = true; });
document.addEventListener('pointerup', () => { pointerDown = false; });
document.addEventListener('pointercancel', () => { pointerDown = false; });
window.addEventListener('blur', () => { pointerDown = false; });
const holdingText = () => pointerDown || !!window.getSelection()?.toString();
const htmlCache = new WeakMap();
function setHtml(id, value) {
  const el = $(id);
  if (htmlCache.get(el) !== value) { el.innerHTML = value; htmlCache.set(el, value); }
}
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = v => v == null ? '未知' : Number(v).toLocaleString('zh-CN');
const seconds = v => v == null ? '—' : `${Number(v).toFixed(2)}s`;
const duration = v => v == null ? '—' : v < 60 ? `${Math.round(v)} 秒` : `${Math.floor(v / 60)} 分 ${Math.round(v % 60)} 秒`;
const phases = ['state 基线','截图基线','Coach 修改与开发测试','候选开发集','基线独立确认','候选独立确认','基线截图审计','候选截图审计'];
const stageNames = ['state 基线','截图基线','候选开发','基线确认','候选确认','基线审计','候选审计'];
const stageName = stage => stage.includes('engineer') ? 'Coach 开发测试 ' + stage.split('-').at(-1) : stageNames[Number(stage.split('-')[1])] || stage;
const states = {evaluating:'对局评测中',waiting_runtime:'对局评测中',engineering:'Coach 正在研究',engineer_testing:'候选开发测试',coaching:'准备复盘',blocked:'运行受阻',complete:'本轮已结束',promoted:'已晋升',rejected:'候选未通过',failed:'本轮失败',stopped:'已停止',stale:'版本已失效'};
const plantNames = {'sunflower':['🌻','向日葵'],'peashooter':['🟢','豌豆射手'],'wall-nut':['🥔','坚果'],'potato mine':['💣','土豆雷'],'cherry bomb':['🍒','樱桃炸弹'],'snow pea':['❄️','寒冰射手'],'repeater':['🌿','双发射手']};
const icon = p => (plantNames[p] || ['🌱',p])[0];
const plantName = p => (plantNames[p] || ['🌱',p])[1];
let latest = null, replayData = null, replayRequest = 0;

function render(d) {
  latest = d;
  const s = d.status || {}, es = d.episodes || [], step = s.step ?? 0;
  const phase = Math.min(7, step < 2 ? step : step === 2 && !s.candidate ? 2 : step + 1);
  const batchKey = 'step-' + step, batch = es.filter(e => e.stage === batchKey);
  const wins = batch.filter(e => e.outcome === 'win').length;
  const known = es.filter(e => Number.isFinite(e.wall_s));
  const mean = known.length ? known.reduce((n,e) => n + e.wall_s, 0) / known.length : null;
  const stat = (label,value,note) => `<div class="stat"><div class="label">${label}</div><div class="big">${value}</div><div class="sub">${note}</div></div>`;
  $('stats').innerHTML = stat('本轮已完成',`${es.length} 局`,'各阶段完整对局记录')
    + stat('当前阶段胜率',batch.length ? (100 * wins / batch.length).toFixed(0) + '%' : '—',`${esc(stageName(batchKey))} · ${wins} 胜 / ${batch.length - wins} 负`)
    + stat('平均单局耗时',duration(mean),'实际耗时 · 仅已完成对局')
    + stat('当前阶段',esc(phases[phase]),esc(states[s.status] || '等待记录'));
  $('runState').textContent = states[s.status] || '等待记录';
  const coaching = ['engineering','coaching'].includes(s.status);
  const completed = s.completed ?? batch.length, total = s.total;
  $('batch').textContent = coaching ? '正在分析已有评测记录；尚未进入下一批正式评测。' : `当前批次 ${completed} / ${total ?? '未知'} 局完成`;
  $('progressBar').style.width = total > 0 && !coaching ? Math.min(100, 100 * completed / total) + '%' : '0%';
  $('track').innerHTML = phases.map((p,i) => `<div class="phase ${i === phase ? 'on' : i < phase ? 'done' : ''}">${i + 1}. ${p}</div>`).join('');
  const raw = s.reason || s.detail || '';
  const endedBadly = ['blocked','failed','rejected','stale','stopped'].includes(s.status);
  $('runReason').hidden = !endedBadly;
  $('runReason').textContent = /RPC connection failed|research process disconnected/.test(raw)
    ? '远程研究连接中断，本轮未完成。已完成对局保留；中断不计为游戏失败。'
    : raw || states[s.status] || '';
  $('technicalReason').hidden = !endedBadly || !raw;
  $('rawReason').textContent = raw;
  $('recordAge').textContent = d.updated ? '任务记录更新于 ' + new Date(d.updated * 1000).toLocaleString('zh-CN') : '尚无任务记录';
  $('baseVersion').textContent = s.parent || d.summary?.before?.version || '未记录';
  $('candidateVersion').textContent = s.candidate || '未生成';
  $('promotion').textContent = s.promoted === true || s.status === 'promoted' ? '已通过并晋升'
    : s.status === 'rejected' ? '候选未通过' : s.candidate ? '尚未通过全部评测' : '尚未评定';
  $('candidateNote').textContent = s.diagnosis || s.rationale || (s.candidate ? '已有候选，等待完整评测结论。' : '尚无候选改动与 Coach 诊断；对局失败原因待复盘，不以连接故障代替战术结论。');
  $('changeDetails').hidden = !s.changes && !s.diagnosis && !s.rationale;
  $('candidateChanges').textContent = [s.diagnosis, s.rationale, s.changes ? JSON.stringify(s.changes,null,2) : ''].filter(Boolean).join('\n\n');
  $('gateDetails').hidden = !s.verdicts?.length;
  $('gates').textContent = JSON.stringify(s.verdicts || [],null,2);

  const groups = [...new Set(es.map(e => e.stage))].sort((a,b) => a.localeCompare(b,undefined,{numeric:true}));
  $('stageResults').innerHTML = groups.length ? groups.map(stage => {
    const games = es.filter(e => e.stage === stage), won = games.filter(e => e.outcome === 'win').length;
    const timed = games.filter(e => Number.isFinite(e.wall_s));
    return `<tr><td>${esc(stageName(stage))}</td><td>${games.length}</td><td>${won} / ${games.length - won}</td><td>${(100 * won / games.length).toFixed(0)}%</td><td>${duration(timed.length ? timed.reduce((n,e) => n + e.wall_s,0) / timed.length : null)}</td><td>${games.filter(e => e.mower_times?.length).length}</td></tr>`;
  }).join('') : '<tr><td colspan="6">尚无完整对局结果</td></tr>';
  const selected = $('stageFilter').value;
  const options = '<option value="">全部阶段</option>' + groups.map(g => `<option value="${esc(g)}">${esc(stageName(g))}</option>`).join('');
  if ($('stageFilter').innerHTML !== options) { $('stageFilter').innerHTML = options; $('stageFilter').value = groups.includes(selected) ? selected : ''; }
  renderEpisodes();
  const calls = (d.coach || []).flatMap(x => x.calls || []);
  const sum = key => calls.length ? calls.some(c => c.usage?.[key] == null) ? null : calls.reduce((n,c) => n + c.usage[key],0) : 0;
  $('coachStatus').textContent = calls.length ? `${calls.length} 次请求 · ${[...new Set(calls.map(c => ({running:'正在推理',queued:'等待处理',completed:'已完成',failed:'失败',cancelled:'已取消'}[c.status] || c.status)))].join(' / ')}` : '尚未触发 · 等待开发集基线完成';
  $('tokens').innerHTML = [['输入','input_tokens'],['其中缓存','cached_input_tokens'],['输出','output_tokens'],['总量','total_tokens']].map(([label,key]) => `<div class="token"><span class="label">${label} token</span><b>${num(sum(key))}</b></div>`).join('');
  $('job').textContent = d.job;
  $('refresh').textContent = '页面每 2 秒刷新 · ' + new Date().toLocaleTimeString();
  $('error').textContent = '';
}

function renderEpisodes() {
  const es = (latest?.episodes || []).filter(e => !$('stageFilter').value || e.stage === $('stageFilter').value);
  $('episodeCount').textContent = `${es.length} 局`;
  $('results').innerHTML = es.length ? [...es].reverse().map(e => `<tr><td>1-${e.level}</td><td>${esc(stageName(e.stage))}</td><td class="${e.outcome === 'win' ? 'win' : 'lose'}">${e.outcome === 'win' ? '胜利' : '失败'}</td><td>${duration(e.elapsed_game_s)} / ${duration(e.wall_s)}</td><td>${e.mower_times?.length ? e.mower_times.map(seconds).join(' / ') : '未触发'}</td><td><button class="review-button" data-stage="${esc(e.stage)}" data-file="${esc(e.file)}">状态复盘</button></td></tr>`).join('') : '<tr><td colspan="6">此阶段暂无完整对局记录</td></tr>';
}

function board(n) {
  const s = n.scene || {}, place = n.placement;
  let grid = '<div></div>' + Array.from({length:9},(_,i) => `<div class="axis">${i + 1}</div>`).join('');
  for (let r = 0; r < (s.rows || 5); r++) {
    grid += `<div class="axis">${r + 1}</div>`;
    for (let c = 0; c < 9; c++) {
      const ps = (n.plants || []).filter(x => x.row === r && x.col === c).flatMap(x => x.plants || []);
      const zs = (s.zombies || []).filter(x => x.row === r && Math.min(8,x.col) === c);
      const aggregate = n.zombie_summary?.[r], count = aggregate?.[1] != null && Math.min(8,aggregate[1]) === c ? aggregate[0] : 0;
      const target = place?.row === r && place?.col === c;
      const marker = target && n.recorded_action ? `<span class="action-marker" title="种植动作标记，不是此观察中已存在的植物">${icon(n.recorded_action.plant)}↘</span>` : '';
      grid += `<div class="cell ${(r + c) % 2 ? 'odd' : ''} ${zs.length || count ? 'hot' : ''} ${target ? 'target' : ''}" title="第${r + 1}行第${c + 1}列 ${esc(ps.map(plantName).join('、'))}${zs.length ? '；僵尸 ' + esc(zs.map(x => x.type).join('、')) : ''}${count ? '；本行共'+count+'只，仅记录最近位置' : ''}">${ps.map(p => `<span>${icon(p)}</span>`).join('')}${zs.length ? `<span class="zombie">🧟${zs.length > 1 ? zs.length : ''}</span>` : ''}${count ? `<span class="zombie">🧟${count}≈</span>` : ''}${marker}</div>`;
    }
  }
  const a = n.recorded_action;
  const action = a ? `${n.action_phase === 'after' ? '关联动作' : '记录动作'}：${seconds(a.tick / 100)} · ${esc(a.source)} 种植 <b>${esc(plantName(a.plant || '未知植物'))}</b> → 第 ${a.placement.row + 1} 行，第 ${a.placement.col + 1} 列`
    : place ? `记录动作：种植 <b>${esc(plantName((s.cards || []).find(c => c.slot === place.slot)?.plant || '植物'))}</b> → 第 ${place.row + 1} 行，第 ${place.col + 1} 列` : '本采样步未记录种植动作；不代表本步调用了 Jev 并决定等待。';
  return `<article class="panel"><div class="boardhead"><h2>历史棋盘</h2><span class="sun">☀ ${num(s.sun)}</span></div><p class="sub">观察时间 ${seconds(s.t_capture)} · 最近决策观察 ${seconds(n.decision_observation_tick == null ? null : n.decision_observation_tick / 100)}</p><div class="legend">🌻 等图标为观察到的植物 · 金框与 ↘ 为种植动作标记${n.observation_kind === 'compact' ? '。🧟N≈ 表示该行总数及最近位置，不代表全部僵尸在此格；种类未记录。' : ' · 🧟 为僵尸'}</div><div class="lawn">${grid}</div><div class="cards">${(s.cards || []).map(c => `<span class="seed ${c.ready ? 'ready' : ''}">${icon(c.plant)} ${esc(plantName(c.plant))} ${c.cost ?? '?'} · ${c.ready ? '可用' : '冷却/不可用'}</span>`).join('')}</div><div class="action">${action}</div><p class="sub">Planner${n.plan_scope ? '（'+esc(n.plan_scope)+'）' : ''}：${esc(n.plan?.objective || '未记录')}</p></article>`;
}

async function replay(stage,file,job) {
  liveView = null;
  const request = ++replayRequest;
  replayData = null;
  $('replayTitle').textContent = '读取状态复盘…';
  $('nodeChooser').replaceChildren(); $('nodeChooser').disabled = true;
  $('nodeBoard').replaceChildren(); $('nodePlan').textContent = ''; $('nodeMeta').textContent = '';
  $('episodeMetrics').textContent = '';
  $('replay').showModal();
  try {
    const r = await fetch('/api/episode?stage=' + encodeURIComponent(stage) + '&file=' + encodeURIComponent(file) + (job ? '&job=' + encodeURIComponent(job) : ''));
    if (!r.ok) throw Error('未找到完整对局记录');
    const data = await r.json();
    if (request !== replayRequest) return;
    replayData = data;
    const throughput = data.throughput || {}, times = throughput.timings || {};
    $('episodeMetrics').textContent = `游戏时间 ${duration(data.elapsed_game_s)} · 实际耗时 ${duration(data.wall_s)}\nPlanner ${num(times.planner_s?.count)} 次 / 累计 ${duration(times.planner_s?.total_s)} · Jev ${num(throughput.jev_requests)} 次模型请求 / 累计 ${duration(times.jev_s?.total_s)}\n决策记录 ${num(throughput.decisions)} 条（不等于模型请求数）`;
    $('replayTitle').textContent = `1-${data.level} · ${data.outcome === 'win' ? '胜利' : '失败'} · ${seconds(data.elapsed_game_s)}`;
    $('nodeChooser').innerHTML = data.nodes.map((n,i) => `<option value="${i}">${seconds(n.tick / 100)} · ${esc(n.label)}</option>`).join('');
    $('nodeChooser').disabled = !data.nodes.length;
    if (data.nodes.length) selectNode(0); else $('nodeMeta').textContent = '此局未保存可重建的状态节点。';
  } catch (e) { if (request === replayRequest) { $('replayTitle').textContent = '读取失败'; $('nodeMeta').textContent = e.message; } }
}

function selectNode(index) {
  const n = replayData.nodes[index];
  $('nodeMeta').textContent = (n.mower_events || []).map(e => `第 ${e.row + 1} 行小推车实际触发：${seconds(e.time_s)}`).join('\n')
    + (n.action_phase === 'before' ? `\n种植动作 ${seconds(n.tick / 100)}；棋盘是动作前最近的已记录观察，箭头图标仅标记种植位置。`
       : n.action_phase === 'after' ? '\n这是该动作之后的已记录观察；植物是否仍在场以此快照为准，不补画已消失的樱桃。'
       : `\n日志采样 ${seconds(n.tick / 100)}。观察可能早于事件；最后一个节点是终局前观察，不是精确终局状态。`);
  $('nodeBoard').innerHTML = board(n);
  $('nodePlan').textContent = n.plan ? (n.plan_scope ? n.plan_scope+'（未保存完整消息）\n' : '') + JSON.stringify(n.plan,null,2) : '未记录';
}
$('stageFilter').addEventListener('change',renderEpisodes);
$('results').addEventListener('click',e => { const b = e.target.closest('button[data-stage]'); if (b) replay(b.dataset.stage,b.dataset.file); });
$('nodeChooser').addEventListener('change',e => selectNode(Number(e.target.value)));
$('closeReplay').addEventListener('click',() => $('replay').close());
$('replay').addEventListener('close',() => { replayRequest++; });
async function update() {
  try { if (holdingText()) return; const r = await fetch('/api/status',{cache:'no-store'}); if (!r.ok) throw Error(r.status); const d = await r.json(); if (!holdingText()) render(d); }
  catch (e) { if (!holdingText()) $('error').textContent = '状态连接中断，保留上次显示：' + e.message; }
  finally { setTimeout(update,2000); }
}
update();

const trainStates = {running:'训练中',starting:'正在启动',complete:'已结束',stopped:'已停止',blocked:'运行受阻',interrupted:'已中断',failed_to_start:'启动失败'};
let trainBusy = false, training = {}, levelData = null, selectedLevel = null, attemptsShown = [];
try { selectedLevel = Number(localStorage.getItem('pvz-level')) || null; } catch (e) { selectedLevel = null; }

function levelState(level) {
  const t = training, entry = levelData?.levels?.[level - 1] || {};
  if ((t.skipped || levelData?.skipped || [1]).includes(level)) return ['skip', '跳过（教学关）'];
  if (t.running && t.level === level) return ['current', t.phase === 'rsi' ? 'Coach 改进中' : '正在打'];
  if ((t.won || []).includes(level) || entry.clean_wins) return ['won', '无小推车赢过'];
  if ((t.mower_wins || []).includes(level) || entry.wins) return ['mower', '赢但用了小推车'];
  if (entry.played) return ['lost', '还没赢过'];
  return ['', '未打'];
}

function defaultLevel() {
  if (training.running && training.level) return training.level;
  const trained = levelData?.trained || [2,3,4,5,6,7,8,9,10];
  return trained.find(l => levelState(l)[0] !== 'won') || trained[0];
}

function renderLevels() {
  const level = selectedLevel || defaultLevel();
  setHtml('levels', Array.from({length:10}, (_,i) => i + 1).map(l => {
    const [cls, note] = levelState(l), entry = levelData?.levels?.[l - 1] || {};
    const counts = entry.played || entry.tries ? `${entry.played || 0} 局 · ${entry.rounds || 0} 轮改进` : '';
    return `<button class="level ${cls}" role="tab" aria-selected="${l === level}" data-level="${l}"><b>1-${l}</b><span>${note}</span>${counts ? `<small>${counts}</small>` : ''}</button>`;
  }).join(''));
  renderLevelDetail(level);
}

const angles = {a: '从代码战术入手', b: '从策略文字和条件经验入手', c: '从出事的那几秒入手'};
const variantName = v => v ? `改法 ${v.toUpperCase()}（${angles[v] || v}）` : '';
const outcomeText = g => g.outcome === 'win' ? (g.mowers ? `赢，但用了 ${g.mowers} 辆小推车` : '赢，没用小推车') : g.outcome === 'lose' ? '输' : '未完成';
const outcomeTag = g => `<span class="tag ${g.outcome === 'win' && !g.mowers ? 'good' : g.outcome === 'win' ? 'warn' : 'bad'}">${outcomeText(g)}</span>`;

function renderLevelDetail(level) {
  const entry = levelData?.levels?.[level - 1];
  const [, note] = levelState(level);
  if (!entry) { $('levelDetail').innerHTML = '<p class="empty">正在读取关卡记录…</p>'; return; }
  const events = [...entry.games.map(g => ({...g, type:'game'})), ...entry.attempts.map(a => ({...a, type:'attempt'}))]
    .sort((a, b) => (a.time || 0) - (b.time || 0) || (a.type === 'game' ? -1 : 1));
  attemptsShown = entry.attempts;
  let gameNo = 0;
  const items = events.map(e => {
    if (e.type === 'game') {
      gameNo += 1;
      const what = e.kind === 'coach_test' ? `Coach 第 ${e.attempt} 轮${e.variant ? ' · ' + variantName(e.variant) : ''}的测试局` : e.older ? '闯关对局（早先的训练）' : '闯关对局';
      const replayButton = e.job && e.stage ? `<button class="review-button" data-job="${esc(e.job)}" data-stage="${esc(e.stage)}" data-file="${esc(e.file)}">复盘这一局</button>` : '';
      return `<div class="event game"><div class="event-head"><b>第 ${gameNo} 局 · ${what}</b>${outcomeTag(e)}</div>`
        + `<p><span>得分 ${e.score == null ? '—' : Number(e.score).toFixed(2)} · 实际用时 ${duration(e.wall_s)}${e.version ? ' · 策略 ' + esc(e.version) : ''}</span></p>`
        + (replayButton ? `<div class="actions">${replayButton}</div>` : '') + '</div>';
    }
    const result = e.result || {};
    const verdict = e.duplicate_of ? '<span class="tag">和另一种改法的改动相同，没有重打</span>'
      : e.cleared ? '<span class="tag good">过关：保留</span>' : e.kept ? '<span class="tag warn">这一轮最好：保留，继续改</span>'
      : e.detail === 'another variant did better' ? '<span class="tag bad">另一种改法更好：不保留</span>'
      : e.version ? '<span class="tag bad">不比原来好：不保留</span>' : `<span class="tag bad">${esc(states[e.status] || e.status || '没有提交改动')}</span>`;
    const files = (e.changes?.files || []).map(esc).join('、');
    const index = entry.attempts.indexOf(entry.attempts.find(a => a.job === e.job));
    return `<div class="event attempt ${e.kept ? 'kept' : 'dropped'}"><div class="event-head"><b>Coach 第 ${e.attempt} 轮${e.variant ? ' · ' + variantName(e.variant) : ''}</b>${verdict}</div>`
      + (e.diagnosis ? `<p><span>发现的问题：</span>${esc(e.diagnosis)}</p>` : '')
      + (e.rationale ? `<p><span>改了什么、为什么：</span>${esc(e.rationale)}</p>` : '')
      + (files ? `<p><span>改动的文件：</span>${files}</p>` : '')
      + (result.outcome ? `<p><span>改完重打同一局：</span>${outcomeText(result)}（得分 ${Number(result.score).toFixed(2)}，测试 ${e.tests ?? 1} 次）</p>` : e.detail ? `<p><span>结果：</span>${esc(e.detail)}</p>` : '')
      + (e.changes?.diff ? `<div class="actions"><button class="review-button" data-diff="${index}">查看具体改动</button></div>` : '') + '</div>';
  });
  const t = training;
  if (t.running && t.level === level && t.phase === 'rsi') {
    const variants = Object.entries(t.variants || {}).sort();
    const lines = variants.length ? variants.map(([v, s]) => `<p><span>${variantName(v)}：</span>${esc(variantStatus(s))}</p>`).join('') : '';
    items.push(`<div class="event attempt running"><div class="event-head"><b>Coach 第 ${t.round} 轮进行中 · 同时试 ${variants.length || 2} 种改法</b></div>${lines}<p><span>每种改法各自修改这一关的策略并重打同一局，最后只保留最好的一种；改动完全相同的不重复重打。</span></p></div>`);
  } else if (t.running && t.level === level && t.phase === 'campaign') {
    items.push(`<div class="event game running"><div class="event-head"><b>第 ${gameNo + 1} 局进行中</b></div></div>`);
  }
  const single = levelState(level)[0] === 'skip' ? '' : `<button class="train-button" data-train-level="${level}" ${training.running || trainBusy ? 'disabled' : ''} title="用一个新的随机种子打这一关；输了或用了小推车就让 Coach 改这关的策略并重打，直到不用小推车赢下">单独训练这一关</button>`;
  setHtml('levelDetail', `<div class="detail-head"><h3>1-${level} · ${note}</h3>${single}</div><div class="detail-head"><div class="counts">`
    + `<span>打了 <b>${entry.played}</b> 局</span><span>赢 <b>${entry.wins}</b> 局，其中不用小推车 <b>${entry.clean_wins}</b> 局</span>`
    + `<span>Coach 改了 <b>${entry.rounds}</b> 轮、共 <b>${entry.tries}</b> 种改法，保留 <b>${entry.kept}</b> 次</span>${entry.version ? `<span>当前策略 <b>${esc(entry.version)}</b></span>` : ''}</div></div>`
    + (items.length ? `<div class="timeline">${items.join('')}</div>` : `<p class="empty">${levelState(level)[0] === 'skip' ? '1-1 是教学关：不种第一株植物就不开始，训练跳过。' : '这一关还没打过。'}</p>`));
}

function variantStatus(s) {
  const labels = {coaching: '准备中', engineering: 'Coach 正在修改', engineer_testing: '重打这一局测试中', tested: '测完，等另一种改法',
                  rejected: '已结束', failed: '失败', promoted: '保留'};
  return (labels[s.status] || states[s.status] || s.status || '准备中') + (s.status === 'rejected' && s.detail ? `（${s.detail}）` : '');
}

function renderTraining(t) {
  training = t;
  const running = !!t.running, last = t.last || {};
  $('trainState').textContent = t.stopping ? '正在停止' : trainStates[t.state] || '尚未开始';
  let now = '';
  if (running && t.phase === 'campaign' && t.level) now = `正在打 1-${t.level}`;
  else if (running && t.phase === 'rsi') now = `1-${esc(t.level ?? last.level ?? '?')} ${last.outcome === 'win' ? `用了 ${last.mowers} 辆小推车` : '没赢'}，Coach 第 ${t.round} 轮：`
    + (Object.entries(t.variants || {}).sort().map(([v, s]) => `改法 ${v.toUpperCase()} ${esc(variantStatus(s))}`).join(' · ') || '准备中');
  else if (t.state === 'starting') now = '正在启动训练';
  else if (!t.state) now = '尚未开始。按「开始训练」，从第一个还没不用小推车赢过的关卡开始。';
  if (running && t.single) now = `单独训练 1-${t.single}（新种子 ${t.seed}） · ` + now;
  if (running && t.notice) now += ` · <b>${esc(t.notice)}</b>`;
  $('trainNow').innerHTML = now;
  $('trainLive').innerHTML = (t.live || []).map(lv => esc(`1-${lv.level} 进行中：游戏时间 ${lv.game_s} 秒 · 已决策 ${lv.decisions ?? 0} 次 · 阳光 ${lv.sun ?? '未知'} · 场上植物 ${lv.plants} 株 · 僵尸 ${lv.zombies} 只`)
    + ` <button class="review-button" data-live-job="${esc(lv.job)}" data-episode="${esc(lv.episode)}">看棋盘</button>`).join('<br>');
  const showMessage = !running && t.message && t.state !== 'running';
  $('trainMessage').hidden = !showMessage;
  const message = showMessage ? t.message : '';
  if ($('trainMessage').textContent !== message) {
    $('trainMessage').textContent = message;
    $('copyStatus').textContent = '';
  }
  $('copyTrainError').hidden = !showMessage;
  $('trainStart').disabled = trainBusy || running;
  $('trainStop').disabled = trainBusy || !running || !!t.stopping;
  renderLevels();
}

async function trainAction(action, level = null) {
  trainBusy = true; renderTraining(training); $('trainError').textContent = '';
  try {
    const r = await fetch('/api/training/' + action + (level ? '?level=' + level : ''), {method:'POST', headers:{'X-PvZ-Control':'1'}}), d = await r.json();
    if (!r.ok) throw Error(d.error || r.status);
    if (action === 'stop') $('trainError').textContent = '已请求停止；当前这一步结束后停下，已完成的对局保留。';
  } catch (e) { $('trainError').textContent = '操作失败：' + e.message; }
  finally { trainBusy = false; await updateTraining(false); }
}
$('trainStart').addEventListener('click', () => trainAction('start'));
$('trainStop').addEventListener('click', () => trainAction('stop'));
$('copyTrainError').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('trainMessage').textContent);
    $('copyStatus').textContent = '已复制';
  } catch (e) {
    const range = document.createRange(); range.selectNodeContents($('trainMessage'));
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    $('copyStatus').textContent = '已选中错误，请按 Ctrl+C 复制';
  }
});
$('levels').addEventListener('click', e => {
  const b = e.target.closest('button[data-level]');
  if (!b) return;
  selectedLevel = Number(b.dataset.level);
  try { localStorage.setItem('pvz-level', String(selectedLevel)); } catch (err) { /* private window */ }
  renderLevels();
});
$('levelDetail').addEventListener('click', e => {
  const single = e.target.closest('button[data-train-level]');
  if (single) return trainAction('start', Number(single.dataset.trainLevel));
  const game = e.target.closest('button[data-job]');
  if (game) return replay(game.dataset.stage, game.dataset.file, game.dataset.job);
  const diff = e.target.closest('button[data-diff]');
  if (diff) {
    const a = attemptsShown[Number(diff.dataset.diff)];
    $('diffTitle').textContent = `1-${levelData && selectedLevel || defaultLevel()} · Coach 第 ${a.attempt} 次尝试的改动`;
    $('diffMeta').textContent = `策略 ${a.base_version || '?'} → ${a.version || '?'}；改动的策略段：${(a.changes.sections || []).join('、') || '无'}`;
    $('diffText').textContent = a.changes.diff;
    $('diffDialog').showModal();
  }
});
$('closeDiff').addEventListener('click', () => $('diffDialog').close());

// A game in progress: its key frames (each planting, at least every 20 game seconds), refreshed while open.
let liveView = null;
async function showLive(job, episode, first = true) {
  if (!first && holdingText()) {
    if (liveView && $('replay').open) setTimeout(() => showLive(job, episode, false), 3000);
    return;
  }
  if (first) {
    liveView = {job, episode, request: ++replayRequest};
    replayData = null;
    $('replayTitle').textContent = '读取正在打的这一局…';
    $('nodeChooser').replaceChildren(); $('nodeBoard').replaceChildren(); $('nodePlan').textContent = ''; $('nodeMeta').textContent = '';
    $('episodeMetrics').textContent = '对局进行中 · 关键帧：每次种植一帧，至少每 20 秒游戏时间一帧 · 每 3 秒刷新';
    $('replay').showModal();
  }
  const view = liveView;
  try {
    const r = await fetch(`/api/live?job=${encodeURIComponent(job)}&episode=${encodeURIComponent(episode)}`, {cache:'no-store'});
    if (!r.ok) throw Error(r.status);
    const data = await r.json();
    if (view !== liveView || view.request !== replayRequest) return;
    if (holdingText()) { setTimeout(() => showLive(job, episode, false), 3000); return; }
    const atNewest = !replayData || Number($('nodeChooser').value) === replayData.nodes.length - 1;
    replayData = data;
    $('replayTitle').textContent = `1-${data.level ?? '?'} · 进行中 · ${data.nodes.length} 个关键帧`;
    $('nodeChooser').innerHTML = data.nodes.map((n,i) => `<option value="${i}">${seconds(n.tick / 100)} · ${esc(n.label)}</option>`).join('');
    $('nodeChooser').disabled = !data.nodes.length;
    if (data.nodes.length) {
      const index = atNewest ? data.nodes.length - 1 : Math.min(Number($('nodeChooser').dataset.kept || 0), data.nodes.length - 1);
      $('nodeChooser').value = String(index); selectNode(index);
    } else $('nodeMeta').textContent = '这一局还没有关键帧。';
  } catch (e) { if (view === liveView) $('nodeMeta').textContent = '读取失败：' + e.message; }
  if (view === liveView && $('replay').open) setTimeout(() => showLive(job, episode, false), 3000);
}
$('nodeChooser').addEventListener('change', e => { e.target.dataset.kept = e.target.value; });
$('replay').addEventListener('close', () => { liveView = null; });
$('trainLive').addEventListener('click', e => {
  const b = e.target.closest('button[data-live-job]');
  if (b) showLive(b.dataset.liveJob, b.dataset.episode);
});
async function updateTraining(repeat = true) {
  try { if (holdingText()) return; const r = await fetch('/api/training', {cache:'no-store'}); if (!r.ok) throw Error(r.status); const d = await r.json(); if (!holdingText()) renderTraining(d); }
  catch (e) { if (!holdingText()) $('trainState').textContent = '读取失败'; }
  finally { if (repeat) setTimeout(updateTraining, 2000); }
}
async function updateLevels() {
  try { if (holdingText()) return; const r = await fetch('/api/levels', {cache:'no-store'}); if (r.ok) { const d = await r.json(); if (!holdingText()) { levelData = d; renderLevels(); } } }
  catch (e) { /* the training panel shows connection problems */ }
  finally { setTimeout(updateLevels, 5000); }
}
updateTraining();
updateLevels();
