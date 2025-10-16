async function startJob(ev) {
  ev.preventDefault();
  const form = ev.target;
  const data = new FormData(form);
  const verbose = !!form.querySelector('#verbose')?.checked;
  const useAuth = !!form.querySelector('#use_auth')?.checked;
  if (!useAuth) {
    data.delete('username');
    data.delete('password');
    data.delete('twofactor');
    data.delete('usenetrc');
    data.set('use_auth', '');
  } else {
    data.set('use_auth', 'on');
  }
  const resp = await fetch('/start', { method: 'POST', body: data });
  const { job_id } = await resp.json();
  document.getElementById('job').classList.remove('hidden');
  document.getElementById('now-state').textContent = 'en progreso';
  const logsEl = document.getElementById('logs');
  logsEl.textContent = '';
  // Always show mini console; server filters lines when verbose is off
  logsEl.classList.remove('hidden');
  // Reset progress bar
  const bar = document.getElementById('progress-bar');
  const ptext = document.getElementById('progress-text');
  if (bar) bar.style.width = '0%';
  if (ptext) ptext.textContent = '0%';

  // Logs via SSE (always open)
  let evt;
  evt = new EventSource(`/logs/${job_id}`);
  evt.onmessage = (e) => {
    logsEl.textContent += e.data + '\n';
    logsEl.scrollTop = logsEl.scrollHeight;
  };
  evt.onerror = () => {
    // keep-alive timeouts are expected, ignore
  };

  // Poll status until finished, then show download
  const downloadLink = document.getElementById('download-link');
  const badge = document.getElementById('now-state');
  async function poll() {
    const s = await fetch(`/status/${job_id}`).then(r => r.json());
    // Update now-playing section
    const nowTitle = document.getElementById('now-title');
    const nowCount = document.getElementById('now-count');
    const title = s.current_title ? s.current_title : '—';
    nowTitle.textContent = `Canción actual: ${title}`;
    const idx = s.current_index || 0, total = s.total || 0;
    nowCount.textContent = `[${idx}/${total}]`;

    // Update progress bar (por pistas)
    if (bar && ptext) {
      let pct = 0;
      if (s.returncode === 0) {
        pct = 100;
      } else if (total > 0) {
        const completed = Math.max(0, (idx || 0) - 1);
        pct = Math.floor((completed / total) * 100);
        if (pct >= 100 && (s.returncode === null || s.returncode === undefined)) {
          pct = 99; // evita 100% antes de cerrar
        }
      }
      bar.style.width = pct + '%';
      ptext.textContent = pct + '%';

      // Also show progress inside console as a single updatable line
      const lines = logsEl.textContent.split('\n');
      // remove trailing empty line artifact
      if (lines.length && lines[lines.length - 1] === '') lines.pop();
      if (lines.length && lines[lines.length - 1]?.startsWith('[PROG]')) {
        lines.pop();
      }
      lines.push(`[PROG] ${pct}%`);
      logsEl.textContent = lines.join('\n') + '\n';
      logsEl.scrollTop = logsEl.scrollHeight;
    }

    // Badge color/state
    const st = s.last_status || (s.returncode == null ? 'downloading' : (s.returncode === 0 ? 'done' : 'error'));
    badge.classList.remove('ok','warn','err');
    if (st === 'done') badge.classList.add('ok');
    else if (st === 'skipped') badge.classList.add('warn');
    else if (st === 'error') badge.classList.add('err');

    if (s.returncode === null || s.returncode === undefined) {
      setTimeout(poll, 1500);
      return;
    }
    badge.textContent = s.returncode === 0 ? 'finalizado' : `error (${s.returncode})`;
    if (evt) evt.close();
    if (s.returncode === 0) {
      if (bar && ptext) { bar.style.width = '100%'; ptext.textContent = '100%'; }
      downloadLink.href = `/download/${job_id}`;
      downloadLink.classList.remove('hidden');
      // Log completion to console
      logsEl.textContent += 'Finalizado.\n';
      logsEl.scrollTop = logsEl.scrollHeight;
    }
  }
  poll();
}

document.getElementById('job-form').addEventListener('submit', startJob);

// Toggle auth block
const useAuthEl = document.getElementById('use_auth');
const authBlock = document.getElementById('auth-block');
if (useAuthEl && authBlock) {
  useAuthEl.addEventListener('change', () => {
    if (useAuthEl.checked) authBlock.classList.remove('hidden');
    else authBlock.classList.add('hidden');
  });
}

