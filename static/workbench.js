(() => {
  const queryForm = document.getElementById('one-line-query-form');
  const queryInput = document.getElementById('one-line-query');
  const resultPanel = document.getElementById('one-line-result');

  const escape = (value) => {
    const node = document.createElement('div');
    node.textContent = value ?? '';
    return node.innerHTML;
  };

  function renderQueryResult(data, question) {
    resultPanel.hidden = false;
    if (!data?.found) {
      resultPanel.innerHTML = `<div class="panel-head"><div><h2>查询结果</h2><p>“${escape(question)}”</p></div></div><p class="query-error">${escape(data?.error || data?.summary || '没有找到匹配记录')}</p>`;
      return;
    }
    const person = data.person?.name || '未关联人员';
    const contract = data.contract?.id || '未关联合同';
    const status = data.status || '待确认';
    const riskLevel = data.risk?.level || '低';
    const needsAction = riskLevel === '高' || riskLevel === '中';
    const nextAction = data.risk?.reason || data.summary || '继续跟进当前流程';
    const detailUrl = data.contract?.id ? `/contracts/${encodeURIComponent(data.contract.id)}` : '#';
    resultPanel.innerHTML = `
      <div class="panel-head"><div><h2>查询结果</h2><p>“${escape(question)}” · ${escape(contract)}</p></div></div>
      <div class="one-line-answer">
        <div><span>当前人员</span><strong>${escape(person)}</strong></div>
        <div><span>当前状态</span><strong class="status-chip" data-status="${escape(status)}">${escape(status)}</strong></div>
        <div><span>下一步建议操作</span><strong>${escape(nextAction)}</strong></div>
        <div><span>是否需要处理</span><strong class="need-${needsAction ? 'yes' : 'no'}">${needsAction ? 'Yes' : 'No'}</strong></div>
        ${detailUrl === '#' ? '' : `<a class="button primary" href="${detailUrl}">打开记录</a>`}
      </div>`;
  }

  queryForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const question = queryInput.value.trim();
    if (!question) return;
    resultPanel.hidden = false;
    resultPanel.innerHTML = '<p class="query-loading">正在查询人员、合同、名额与事件…</p>';
    try {
      const data = await window.labourApi.request('/api/ai/ask', {
        method: 'POST',
        body: JSON.stringify({question}),
      });
      renderQueryResult(data, question);
    } catch (error) {
      renderQueryResult({found: false, summary: error.message}, question);
    }
  });

  document.querySelectorAll('[data-card-edit]').forEach((button) => {
    button.addEventListener('click', () => {
      const editor = document.getElementById(button.dataset.cardEdit);
      if (!editor) return;
      editor.hidden = !editor.hidden;
      button.textContent = editor.hidden ? '内联编辑' : '收起编辑';
      if (!editor.hidden) editor.querySelector('input, select')?.focus();
    });
  });

  document.querySelectorAll('[data-card-cancel]').forEach((button) => {
    button.addEventListener('click', () => {
      const editor = button.closest('.card-editor');
      if (!editor) return;
      editor.hidden = true;
      const trigger = document.querySelector(`[data-card-edit="${editor.id}"]`);
      if (trigger) trigger.textContent = '内联编辑';
    });
  });

  document.querySelectorAll('[data-json-form]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const submit = form.querySelector('button[type="submit"], button:not([type])');
      const data = Object.fromEntries(new FormData(form).entries());
      const oldMessage = form.querySelector('.inline-save-message');
      oldMessage?.remove();
      if (submit) submit.disabled = true;
      try {
        const result = await window.labourApi.request(form.action, {
          method: form.dataset.method || 'POST',
          body: JSON.stringify(data),
        });
        form.insertAdjacentHTML('beforeend', `<p class="inline-save-message">${escape(result?.message || '保存成功，正在刷新…')}</p>`);
        window.setTimeout(() => window.location.reload(), 350);
      } catch (error) {
        form.insertAdjacentHTML('beforeend', `<p class="inline-save-message error">${escape(error.message || '保存失败')}</p>`);
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-delete-resource]').forEach((button) => {
    button.addEventListener('click', async () => {
      const label = button.dataset.deleteLabel || '这条记录';
      if (!window.confirm(`确认删除${label}？删除后将不再出现在业务列表中。`)) return;
      button.disabled = true;
      try {
        await window.labourApi.request(
          `/api/${encodeURIComponent(button.dataset.deleteResource)}/${encodeURIComponent(button.dataset.deleteId)}`,
          {method: 'DELETE'},
        );
        const row = button.closest('article, tr');
        if (row) row.remove();
      } catch (error) {
        window.alert(error.message || '删除失败，请稍后重试。');
        button.disabled = false;
      }
    });
  });

  const personLabel = (person) => `${person.name} · ${person.company_name || '公司待补充'}`;
  const initializePersonBinding = (binding) => {
    const input = binding.querySelector('[data-person-search]');
    const personId = binding.querySelector('[data-person-id]');
    const candidates = binding.querySelector('[data-person-candidates]');
    const selected = binding.querySelector('[data-selected-person]');
    const fallback = binding.querySelector('[data-person-fallback]');
    const message = binding.querySelector('[data-person-binding-message]');
    const createPanel = binding.querySelector('[data-quick-person-create]');
    const form = binding.closest('form');
    const hiddenField = (name, value = '') => {
      let field = form.querySelector(`input[name="${name}"]`);
      if (!field) {
        field = document.createElement('input');
        field.type = 'hidden';
        field.name = name;
        form.append(field);
      }
      if (!field.value) field.value = value;
      return field;
    };
    const bindingSource = hiddenField('person_binding_source', 'manual_input');
    const bindingConfidence = hiddenField('person_binding_confidence', '1');
    let timer;

    const bindPerson = (person, source = '人工确认', sourceCode = 'manual_override') => {
      personId.value = person.id;
      input.value = person.name;
      input.setCustomValidity('');
      selected.textContent = `已绑定：${personLabel(person)}${person.recent_contract ? ` · 最近合同 ${person.recent_contract}` : ''}`;
      selected.hidden = false;
      candidates.hidden = true;
      message.textContent = source;
      message.hidden = false;
      fallback.value = String(person.id);
      bindingSource.value = sourceCode;
      bindingConfidence.value = String(person.confidence ?? (sourceCode === 'manual_override' ? 1 : 0));
      binding.dispatchEvent(new CustomEvent('person-bound', {detail: person}));
    };

    const renderCandidates = (people, heading = '') => {
      candidates.replaceChildren();
      if (heading) {
        const title = document.createElement('strong');
        title.className = 'candidate-heading';
        title.textContent = heading;
        candidates.append(title);
      }
      people.forEach((person) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'person-candidate';
        const confidence = Math.round(Number(person.confidence ?? 0) * 100);
        button.innerHTML = `<strong>${escape(person.name)}</strong><span>${escape(person.company_name || '公司待补充')}</span><small>${escape(person.recent_contract ? `最近合同：${person.recent_contract}` : '暂无合同')} · 匹配度 ${confidence}%</small>`;
        button.addEventListener('click', () => bindPerson(person, '已人工确认候选人员', 'manual_override'));
        candidates.append(button);
      });
      candidates.hidden = people.length === 0;
    };

    const search = async (keyword) => {
      if (!keyword.trim()) {
        candidates.hidden = true;
        return;
      }
      try {
        renderCandidates(await window.labourApi.request(`/api/people/search?q=${encodeURIComponent(keyword)}`));
      } catch (error) {
        message.textContent = error.message || '人员搜索失败';
        message.hidden = false;
      }
    };

    input.addEventListener('input', () => {
      personId.value = '';
      selected.hidden = true;
      clearTimeout(timer);
      timer = setTimeout(() => search(input.value), 180);
    });
    fallback.addEventListener('change', () => {
      const option = fallback.options[fallback.selectedIndex];
      if (option.value) bindPerson({id: option.value, name: option.dataset.name, company_name: option.dataset.company, confidence: 1}, '已从完整名单人工确认', 'manual_override');
    });
    binding.querySelector('[data-create-person]').addEventListener('click', () => {
      createPanel.hidden = !createPanel.hidden;
      if (!createPanel.hidden) createPanel.querySelector('[data-new-person-name]').value = input.value.trim();
    });
    createPanel.querySelectorAll('input, select').forEach((field) => field.addEventListener('input', () => {
      createPanel.dataset.allowDuplicate = 'false';
    }));
    binding.querySelector('[data-confirm-create-person]').addEventListener('click', async () => {
      const name = createPanel.querySelector('[data-new-person-name]').value.trim();
      const gender = createPanel.querySelector('[data-new-person-gender]').value;
      const company_name = createPanel.querySelector('[data-new-person-company]').value.trim();
      if (!window.confirm(`确认创建新人员“${name || '未填写'}”并绑定当前资料？`)) return;
      try {
        const person = await window.labourApi.request('/api/people/quick-create', {
          method: 'POST', body: JSON.stringify({
            name, gender, company_name,
            allow_duplicate: createPanel.dataset.allowDuplicate === 'true',
          }),
        });
        const option = new Option(personLabel(person), person.id);
        option.dataset.name = person.name;
        option.dataset.company = person.company_name || '';
        fallback.add(option);
        bindPerson(person, '新人员已创建并绑定', 'manual_input');
        createPanel.hidden = true;
      } catch (error) {
        if (Array.isArray(error.data) && error.data.length) {
          renderCandidates(error.data, '发现疑似重复人员，请选择已有人员，或再次确认创建');
          createPanel.dataset.allowDuplicate = 'true';
        }
        message.textContent = error.message || '创建人员失败';
        message.hidden = false;
      }
    });

    const autoMatch = async () => {
      const filenames = Array.from(binding.closest('form').querySelectorAll('[data-smart-person-file]'))
        .flatMap((fileInput) => Array.from(fileInput.files || []).map((file) => file.webkitRelativePath || file.name));
      const metadata = Array.from(binding.closest('form').querySelectorAll('[data-smart-person-metadata]'))
        .map((field) => field.value).join(' ');
      const clue = `${filenames.join(' ')} ${metadata}`.trim();
      if (!clue) return;
      const matches = await window.labourApi.request(`/api/person-documents/suggest?filename=${encodeURIComponent(clue)}`);
      if (matches.length === 1) {
        bindPerson(matches[0], '已根据文件名 / metadata 唯一匹配并自动绑定', 'filename_recognition');
      } else if (matches.length > 1) {
        personId.value = '';
        renderCandidates(matches, '匹配到多个人员，请人工确认');
        message.textContent = '存在多人匹配，确认候选后才能上传。';
        message.hidden = false;
      } else {
        message.textContent = '未自动识别人员，请输入关键词搜索、从名单选择或创建新人员。';
        message.hidden = false;
      }
    };
    binding.closest('form').querySelectorAll('[data-smart-person-file], [data-smart-person-metadata]')
      .forEach((field) => field.addEventListener('change', autoMatch));
    binding.closest('form').addEventListener('submit', (event) => {
      if (!personId.value) {
        event.preventDefault();
        input.setCustomValidity('请确认绑定人员后再上传');
        input.reportValidity();
      }
    });
    return {personId, fallback, bindPerson};
  };

  document.querySelectorAll('[data-smart-person-binding]').forEach(initializePersonBinding);

  document.querySelectorAll('[data-batch-upload]').forEach((form) => {
    const inputs = Array.from(form.querySelectorAll('input[type="file"][name="files"]'));
    const countLabel = form.querySelector('[data-upload-file-count]');
    const binding = form.querySelector('[data-smart-person-binding]');
    const personId = binding?.querySelector('[data-person-id]');
    const caseSelect = form.querySelector('[data-batch-case]');
    const suggestion = form.querySelector('[data-batch-suggestion]');
    const files = () => inputs.flatMap((input) => Array.from(input.files || []));
    const refresh = () => {
      const selected = files();
      if (countLabel) countLabel.textContent = selected.length ? `已选择 ${selected.length} 个文件` : '尚未选择文件';
      refreshCases();
    };
    inputs.forEach((input) => input.addEventListener('change', refresh));
    const refreshCases = () => {
      if (!personId || !caseSelect) return;
      caseSelect.value = '';
      Array.from(caseSelect.options).forEach((option) => {
        if (!option.dataset.personId) return;
        option.hidden = option.dataset.personId !== personId.value;
      });
    };
    binding?.addEventListener('person-bound', refreshCases);
    refreshCases();
    form.addEventListener('submit', (event) => {
      const selected = files();
      const oversized = selected.find((file) => file.size > 25 * 1024 * 1024);
      if (!selected.length || selected.length > 50 || oversized) {
        event.preventDefault();
        window.alert(!selected.length ? '请至少选择一个文件。' : selected.length > 50 ? '单次最多上传50个文件。' : `${oversized.name} 超过25MB。`);
        return;
      }
      if (personId?.value) {
        const selectedPerson = binding.querySelector('[data-selected-person]').textContent;
        const visibleCases = Array.from(caseSelect?.options || []).filter((option) => option.dataset.personId === personId.value);
        if (visibleCases.length > 1 || !caseSelect?.value) {
          const selectedCase = caseSelect?.value ? caseSelect.options[caseSelect.selectedIndex].textContent : '暂不绑定 / 系统建议';
          const confirmed = window.confirm(`请二次确认资料归档：\n${selectedPerson}\n办理周期：${selectedCase}\n\n确认继续上传？`);
          if (!confirmed) event.preventDefault();
        }
      }
    });
  });

  const workerBadge = document.querySelector('[data-worker-status]');
  const notificationPanel = document.querySelector('[data-notification-panel]');
  const notificationList = document.querySelector('[data-notification-list]');
  const notificationCount = document.querySelector('[data-notification-count]');

  async function refreshSystemIndicators() {
    try {
      const status = await window.labourApi.request('/api/system/worker-status');
      if (workerBadge) {
        workerBadge.textContent = `Worker ${status.status}`;
        workerBadge.classList.toggle('running', status.status === 'running');
        workerBadge.classList.toggle('stopped', status.status !== 'running');
      }
      const notifications = await window.labourApi.request('/api/notifications');
      if (notificationCount) notificationCount.textContent = notifications.length;
      if (notificationList) {
        notificationList.innerHTML = notifications.length
          ? notifications.map((item) => `<article><strong>${escape(item.title)}</strong><p>${escape(item.message)}</p><small>${escape(item.created_at)}</small></article>`).join('')
          : '<p class="empty-block">暂无通知</p>';
      }
    } catch (_error) {
      if (workerBadge) workerBadge.textContent = 'Worker unknown';
    }
  }
  document.querySelectorAll('[data-notification-toggle]').forEach((button) => button.addEventListener('click', () => {
    if (notificationPanel) notificationPanel.hidden = !notificationPanel.hidden;
  }));
  refreshSystemIndicators();
  window.setInterval(refreshSystemIndicators, 30000);

  const mergeDialog = document.getElementById('merge-candidate-dialog');
  const mergeHost = mergeDialog?.querySelector('[data-merge-candidates]');
  document.querySelectorAll('[data-merge-person]').forEach((button) => button.addEventListener('click', async () => {
    mergeDialog?.showModal();
    if (mergeHost) mergeHost.innerHTML = '<p class="empty-block">正在检查…</p>';
    try {
      const result = await window.labourApi.request(`/api/people/${encodeURIComponent(button.dataset.mergePerson)}/merge-candidates`);
      if (!result.candidates.length) {
        mergeHost.innerHTML = '<p class="empty-block">未发现匹配度超过 85% 的疑似重复人员。</p>';
        return;
      }
      mergeHost.replaceChildren();
      result.candidates.forEach((candidate) => {
        const article = document.createElement('article');
        article.className = 'merge-candidate-card';
        article.innerHTML = `<div><strong>${escape(candidate.name)}</strong><span>${escape(candidate.company_name || '公司待补充')}</span><small>匹配度 ${Math.round(candidate.confidence * 100)}% · ${escape((candidate.reasons || []).join('、'))}</small></div><button type="button" class="button secondary">人工确认合并</button>`;
        article.querySelector('button').addEventListener('click', async () => {
          if (!window.confirm(`确认将“${button.dataset.personName}”合并到“${candidate.name}”？此操作可回滚。`)) return;
          try {
            const workflow = await window.labourApi.request('/api/merge-workflows', {
              method: 'POST', body: JSON.stringify({source_person_id: button.dataset.mergePerson, target_person_id: candidate.id}),
            });
            await window.labourApi.request(`/api/merge-workflows/${encodeURIComponent(workflow.workflow_id)}/confirm`, {method: 'POST'});
            window.location.reload();
          } catch (error) {
            window.alert(error.message || '合并失败，数据未改变。');
          }
        });
        mergeHost.append(article);
      });
    } catch (error) {
      mergeHost.innerHTML = `<p class="query-error">${escape(error.message || '查重失败')}</p>`;
    }
  }));
})();
