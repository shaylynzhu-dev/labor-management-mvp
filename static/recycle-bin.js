document.querySelectorAll('[data-restore-resource]').forEach((button) => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await window.labourApi.request(`/api/${encodeURIComponent(button.dataset.restoreResource)}/${encodeURIComponent(button.dataset.restoreId)}/restore`, {method: 'POST'});
      button.closest('tr')?.remove();
    } catch (error) {
      window.alert(error.message || '恢复失败');
      button.disabled = false;
    }
  });
});

document.querySelectorAll('[data-permanent-resource]').forEach((button) => {
  button.addEventListener('click', async () => {
    if (!window.confirm('永久删除后无法恢复，确认继续？')) return;
    button.disabled = true;
    try {
      await window.labourApi.request(`/api/${encodeURIComponent(button.dataset.permanentResource)}/${encodeURIComponent(button.dataset.permanentId)}/permanent`, {method: 'DELETE'});
      button.closest('tr')?.remove();
    } catch (error) {
      window.alert(error.message || '永久删除失败');
      button.disabled = false;
    }
  });
});
