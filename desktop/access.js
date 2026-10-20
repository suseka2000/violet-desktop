'use strict';
document.getElementById('cancel').addEventListener('click', () => window.accessForm.cancel());
document.getElementById('accessForm').addEventListener('submit', async event => {
  event.preventDefault();
  const input = document.getElementById('token');
  const token = input.value; input.value = '';
  const result = await window.accessForm.save(token);
  if (!result.ok) document.getElementById('error').textContent = result.message;
});
