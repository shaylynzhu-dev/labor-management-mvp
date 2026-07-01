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

  const documentFile = document.querySelector('[data-person-document-file]');
  const documentPerson = document.querySelector('[data-person-document-person]');
  const documentSuggestion = document.querySelector('[data-person-document-suggestion]');
  documentFile?.addEventListener('change', () => {
    const filename = documentFile.files?.[0]?.name || '';
    const matches = Array.from(documentPerson?.options || []).filter(
      (option) => option.dataset.personName && filename.includes(option.dataset.personName),
    );
    if (matches.length === 1) {
      documentPerson.value = matches[0].value;
      documentSuggestion.textContent = `已按文件名推荐绑定：${matches[0].dataset.personName}`;
      documentSuggestion.hidden = false;
    } else if (documentSuggestion) {
      documentSuggestion.hidden = true;
    }
  });
})();
