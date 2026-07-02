const navItems = document.querySelectorAll('.nav-item');
const sections = document.querySelectorAll('[data-section]');
const inlineEditorHost = document.getElementById('inline-editor-host');

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    cache: 'no-store',
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.body ? {'Content-Type': 'application/json'} : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok || payload.code !== 0) {
    const error = new Error(payload.message || '请求失败');
    error.data = payload.data;
    error.code = payload.code;
    throw error;
  }
  return payload.data;
}

window.labourApi = {request: apiFetch};

function openInlineEditor(id) {
  const editor = document.getElementById(id);
  if (!editor) return;
  document.querySelectorAll('dialog[open]').forEach((item) => item.close());
  if (inlineEditorHost) {
    inlineEditorHost.append(editor);
    inlineEditorHost.hidden = false;
    editor.show();
  } else {
    editor.showModal();
  }
  editor.querySelector('input, select, textarea, button')?.focus();
  inlineEditorHost.scrollIntoView({behavior: 'smooth', block: 'start'});
}

navItems.forEach((item) => item.addEventListener('click', () => {
  navItems.forEach((nav) => nav.classList.toggle('active', nav === item));
  const view = item.dataset.view;
  sections.forEach((section) => {
    section.hidden = !section.dataset.section.split(' ').includes(view);
  });
}));

document.querySelector('.nav-item.active')?.scrollIntoView({block: 'nearest', inline: 'center'});

document.querySelectorAll('[data-open]').forEach((button) => {
  button.addEventListener('click', () => openInlineEditor(button.dataset.open));
});

document.querySelectorAll('[data-close]').forEach((button) => {
  button.addEventListener('click', () => {
    button.closest('dialog')?.close();
    if (inlineEditorHost) inlineEditorHost.hidden = true;
  });
});

document.querySelectorAll('[data-event-person]').forEach((button) => {
  button.addEventListener('click', () => {
    const dialog = document.getElementById('event-modal');
    const select = dialog?.querySelector('select[name="person_id"]');
    if (select) select.value = button.dataset.eventPerson;
    openInlineEditor('event-modal');
  });
});

const aiForm = document.getElementById('ai-question-form');
const aiQuestion = document.getElementById('ai-question');
const aiAnswer = document.getElementById('ai-answer');
const aiLoading = document.getElementById('ai-loading');

function escapeHtml(value) {
  const element = document.createElement('div');
  element.textContent = value ?? '';
  return element.innerHTML;
}

function renderAiAnswer(data) {
  if (!data.found) {
    aiAnswer.innerHTML = `<div class="ai-empty"><strong>${escapeHtml(data.error || data.summary)}</strong><p>${escapeHtml(data.suggestion || '请换一种方式提问。')}</p></div>`;
    return;
  }
  const events = data.recent_events.length
    ? data.recent_events.map((event) => `<li><span>${escapeHtml(event.type)}</span><div>${escapeHtml(event.note)}<small>${escapeHtml(event.time)}</small></div></li>`).join('')
    : '<li class="no-event">暂无事件记录</li>';
  aiAnswer.innerHTML = `
    <div class="answer-summary"><span>AI 总结</span><strong>${escapeHtml(data.summary)}</strong></div>
    <div class="answer-grid">
      <article><span>当前人员</span><strong>${escapeHtml(data.person.name)}</strong><small>证件后四位 ${escapeHtml(data.person.id_last4)} · 通行证 ${escapeHtml(data.person.permit_last4)}</small></article>
      <article><span>合同周期</span><strong>${escapeHtml(data.contract.start_date)} 至 ${escapeHtml(data.contract.end_date)}</strong><small>${escapeHtml(data.contract.id)} · ${escapeHtml(data.contract.company)}</small></article>
      <article><span>剩余天数 / 状态</span><strong class="answer-status">${escapeHtml(data.contract.remaining_label)} · ${escapeHtml(data.status)}</strong><small>以合同结束日期计算</small></article>
      <article class="risk risk-${escapeHtml(data.risk.level)}"><span>风险判断</span><strong>${escapeHtml(data.risk.level)}风险 · ${escapeHtml(data.risk.label)}</strong><small>${escapeHtml(data.risk.reason)}</small></article>
    </div>
    <div class="answer-events"><h3>最近事件</h3><ul>${events}</ul></div>`;
}

if (aiForm) {
  aiForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const question = aiQuestion.value.trim();
    if (!question) return;
    aiLoading.hidden = false;
    aiAnswer.hidden = true;
    try {
      const data = await apiFetch('/api/ai/ask', {
        method: 'POST',
        body: JSON.stringify({question}),
      });
      renderAiAnswer(data);
    } catch (error) {
      renderAiAnswer({found: false, summary: error.message || '查询失败，请稍后重试。'});
    } finally {
      aiLoading.hidden = true;
      aiAnswer.hidden = false;
    }
  });
}

document.querySelectorAll('[data-question]').forEach((button) => {
  button.addEventListener('click', () => {
    aiQuestion.value = button.dataset.question;
    aiQuestion.focus();
  });
});

const quotaUsageDialog = document.getElementById('quota-usage-modal');
const quotaUsageForm = document.getElementById('quota-usage-form');
const quotaUsageLabel = document.getElementById('quota-usage-label');

document.querySelectorAll('[data-assign-quota]').forEach((button) => {
  button.addEventListener('click', () => {
    quotaUsageForm.action = `/quotas/${button.dataset.assignQuota}/assign`;
    quotaUsageLabel.textContent = button.dataset.quotaLabel;
    openInlineEditor('quota-usage-modal');
  });
});

async function refreshBusinessStatus() {
  try {
    const data = await apiFetch('/api/dashboard/status');
    if (!data) return;
    document.querySelectorAll('[data-live-risk]').forEach((node) => { node.textContent = data.risks.open; });
    document.querySelectorAll('[data-live-high-risk]').forEach((node) => { node.textContent = data.risks.high; });
    document.querySelectorAll('[data-live-medium-risk]').forEach((node) => { node.textContent = data.risks.medium; });
    document.querySelectorAll('[data-live-low-risk]').forEach((node) => { node.textContent = data.risks.low; });
    document.querySelectorAll('[data-live-task]').forEach((node) => { node.textContent = data.tasks.open; });
    document.querySelectorAll('[data-live-overdue-task]').forEach((node) => { node.textContent = data.tasks.overdue; });
    document.querySelectorAll('[data-live-contracts-90]').forEach((node) => { node.textContent = data.contracts_90; });
    document.querySelectorAll('[data-live-certificate-risk]').forEach((node) => { node.textContent = data.certificate_risks; });
    document.querySelectorAll('[data-live-time]').forEach((node) => { node.textContent = data.updated_at; });
  } catch (_error) {
    // The next scheduled refresh retries silently; normal page operations remain available.
  }
}

refreshBusinessStatus();
window.setInterval(refreshBusinessStatus, 30000);

const pushTray = document.getElementById('push-tray');
if (pushTray) {
  let lastNotificationKey = null;
  async function refreshReminders() {
    try {
      const reminders = await apiFetch('/stream/reminders');
      document.querySelectorAll('.push-status').forEach((node) => { node.textContent = '提醒已更新'; });
    pushTray.innerHTML = reminders.slice(0, 3).map((item) => `
      <a class="push-message push-${escapeHtml(item.level)}" href="${escapeHtml(item.url)}">
        <span>${escapeHtml(item.level)}</span>
        <div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.message)}</small></div>
      </a>`).join('');
      if (reminders.length && reminders[0].key !== lastNotificationKey && window.Notification?.permission === 'granted') {
      const item = reminders[0];
      new Notification(item.title, {body: item.message, tag: item.key});
        lastNotificationKey = item.key;
      }
    } catch (_error) {
      document.querySelectorAll('.push-status').forEach((node) => { node.textContent = '提醒稍后重试'; });
    }
  }
  refreshReminders();
  window.setInterval(refreshReminders, 30000);
}
