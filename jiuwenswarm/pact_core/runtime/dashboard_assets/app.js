const byId = id => document.getElementById(id);
let fingerprint = '';
let refreshInFlight = false;
const labels = {active: '进行中', blocked: '待处理', paused: '已暂停', completed: '已交付', passed: '通过', failed: '失败', unavailable: '不可用'};
function panel(id, title, lines) {
  const target = byId(id);
  const heading = document.createElement('h2');
  heading.textContent = title;
  target.replaceChildren(heading);
  for (const line of lines) {
    const text = document.createElement('p');
    text.className = 'secondary';
    text.style.whiteSpace = 'pre-wrap';
    text.textContent = line;
    target.append(text);
  }
}
function render(data) {
  const ready = data.availability === 'ready';
  byId('dashboard').hidden = !ready;
  byId('empty-state').hidden = ready;
  byId('connection-state').textContent = ready ? '工作状态已连接' : `视图状态：${data.availability}`;
  if (!ready) return;
  const view = data.overview;
  panel('overview-panel', `${view.work_id} · ${labels[view.status] || view.status}`, [view.goal, `工作版本 ${view.revision} · 目标版本 ${view.contract_revision}`]);
  panel('candidate-panel', '当前候选', [data.candidate.summary, `检查点：${data.candidate.checkpoint}`]);
  const verification = data.verification;
  const lines = verification ? [labels[verification.status] || verification.status, `退出码：${verification.exit_code ?? '未记录'}`, verification.stdout_tail || '', verification.stderr_tail || ''] : ['尚未验证当前候选'];
  if (data.operation) lines.push(`未确认操作：${data.operation.action} / ${data.operation.phase}。进程是否仍在运行尚未确认。`);
  panel('verification-panel', '验证与执行', lines);
  const accepted = data.acceptance;
  panel('acceptance-panel', accepted.delivered ? '已验收并交付' : accepted.accepted ? '已验收，等待交付' : '等待最终验收', accepted.criteria.map(item => `${item.id}：${item.text}`));
  const questions = data.questions.map(item => `${item.assumption}\n依据：${item.basis}`);
  panel('questions-panel', '待用户确认', questions.length ? questions : ['暂无待确认问题']);
  panel('artifacts-panel', '证据记录', data.artifacts);
}
async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch('/api/projection', {cache: 'no-store'});
    if (!response.ok) throw new Error('projection unavailable');
    const data = await response.json();
    const next = JSON.stringify({...data, generated_at: null});
    if (next !== fingerprint) { render(data); fingerprint = next; }
    byId('generated-at').textContent = data.generated_at || '';
  } catch (_) {
    fingerprint = '';
    byId('connection-state').textContent = '视图暂不可用';
  } finally { refreshInFlight = false; }
}
refresh();
setInterval(refresh, 1000);
