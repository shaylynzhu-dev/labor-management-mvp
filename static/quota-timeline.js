(() => {
  const host = document.querySelector('[data-history-view]');
  const timeline = document.querySelector('[data-quota-timeline]');
  if (!host || !timeline) return;

  const escape = (value) => {
    const node = document.createElement('div');
    node.textContent = value ?? '';
    return node.innerHTML;
  };
  const labels = {
    initial: 'Initial', replacement: 'Replacement', resignation: 'Resignation', renewal: 'Renewal',
  };
  let loaded = false;

  function render(items) {
    if (!items.length) {
      timeline.innerHTML = '<div class="timeline-state"><p>暂无人员生命周期记录。</p></div>';
      return;
    }
    timeline.innerHTML = `<ol class="quota-timeline">${items.map((item, index) => {
      const eventType = ['initial', 'replacement', 'resignation', 'renewal'].includes(item.event_type) ? item.event_type : 'initial';
      const isActive = item.status === 'active';
      const round = Number(item.replacement_round || 0);
      const eventLabel = eventType === 'replacement' && round ? `Replacement #${round}` : labels[eventType];
      const transition = index ? `<div class="timeline-transition">${escape(labels[eventType])}</div>` : '';
      return `<li class="timeline-item ${isActive ? 'is-active' : 'is-ended'} is-${eventType}">
        <span class="timeline-marker" aria-hidden="true"></span>
        ${transition}
        <article class="timeline-card">
          <div class="timeline-person"><strong>${escape(item.worker_name || '未命名人员')}</strong><small>${escape(eventLabel)}</small></div>
          <div class="timeline-badges"><span class="timeline-badge badge-${eventType}">${escape(eventLabel)}</span>${isActive ? '<span class="timeline-badge badge-current">Current</span>' : ''}</div>
          <div class="timeline-dates"><time>${escape(item.start_date || '待确认')}</time><span class="timeline-arrow" aria-hidden="true"></span><time>${escape(item.end_date || (isActive ? 'now' : '待确认'))}</time></div>
          <div class="timeline-status ${isActive ? 'is-active' : ''}">${isActive ? 'active' : 'ended'}</div>
        </article>
      </li>`;
    }).join('')}</ol>`;
  }

  async function loadTimeline(force = false) {
    if (loaded && !force) return;
    timeline.innerHTML = '<div class="timeline-state"><span class="timeline-spinner" aria-hidden="true"></span><p>正在整理生命周期…</p></div>';
    try {
      const items = await window.labourApi.request(timeline.dataset.timelineUrl);
      render(items || []);
      loaded = true;
    } catch (error) {
      timeline.innerHTML = `<div class="timeline-state"><p>${escape(error.message || '时间轴加载失败')}</p><button class="timeline-retry" type="button">重新加载</button></div>`;
      timeline.querySelector('.timeline-retry')?.addEventListener('click', () => loadTimeline(true));
    }
  }

  host.querySelectorAll('[data-history-tab]').forEach((tab) => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.historyTab;
      host.querySelectorAll('[data-history-tab]').forEach((item) => {
        const active = item === tab;
        item.classList.toggle('is-active', active);
        item.setAttribute('aria-selected', String(active));
      });
      host.querySelectorAll('[data-history-panel]').forEach((panel) => {
        panel.hidden = panel.dataset.historyPanel !== target;
      });
      if (target === 'timeline') loadTimeline();
    });
  });
})();
