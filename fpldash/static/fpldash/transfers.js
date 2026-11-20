// ── TRANSFERS TAB ──────────────────────────────────────────
// Renders /api/transfers for the My Squad team: up to 5 suggested swaps
// (out → in) scored on expected points over the next few GWs, plus the
// whole squad's outlook so the reasoning is visible. See fpldash/transfers.py.
(function () {
  const STRENGTH = {
    strong:   { label: 'Strong · worth a -4 hit', cls: 'strong' },
    good:     { label: 'Good · use a free transfer', cls: 'good' },
    marginal: { label: 'Marginal · only with a spare FT', cls: 'marginal' },
  };
  const FALLBACK_PHOTO = 'https://cdn-icons-png.flaticon.com/512/149/149071.png';
  let loaded = false;

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function photo(p) {
    return p.photo
      ? `https://resources.premierleague.com/premierleague/photos/players/250x250/p${p.photo}.png`
      : FALLBACK_PHOTO;
  }

  function chanceTag(p) {
    if (p.chance >= 100) return '';
    return `<span class="tx-flag" title="${esc(p.news)}">${p.chance}%</span>`;
  }

  function playerSide(p, kind) {
    return `
      <div class="tx-player ${kind}">
        <img src="${photo(p)}" alt="" onerror="this.src='${FALLBACK_PHOTO}'">
        <div class="tx-pinfo">
          <div class="tx-pname">${esc(p.name)} ${chanceTag(p)}</div>
          <div class="tx-psub">${posBadge(p.position)} ${esc(p.team)} · £${p.price.toFixed(1)}m</div>
          <div class="tx-pstats">
            <span title="ML predicted points per game">Pred <b>${p.pred.toFixed(2)}</b></span>
            <span title="Average fixture difficulty, next 3 GWs">FDR ${fdrBadge(p.fdr)}</span>
            <span title="Expected points over the next 3 GWs">xPts <b>${p.xpts.toFixed(1)}</b></span>
          </div>
        </div>
      </div>`;
  }

  function suggestionCard(s, i) {
    const st = STRENGTH[s.strength] || STRENGTH.marginal;
    const cost = s.cost_change > 0 ? `+£${s.cost_change.toFixed(1)}m` : s.cost_change < 0
      ? `−£${(-s.cost_change).toFixed(1)}m` : '£0.0m';
    const bench = s.starter ? '' : '<span class="tx-note">currently on your bench</span>';
    const alts = s.alternatives.length
      ? `<div class="tx-alts">Also consider: ${s.alternatives.map(a =>
          `<span class="tx-alt">${esc(a.name)} <small>${esc(a.team)} £${a.price.toFixed(1)}m · +${a.gain.toFixed(1)}</small></span>`).join('')}</div>`
      : '';
    return `
      <div class="tx-card ${st.cls}" style="animation-delay:${i * 90}ms">
        <div class="tx-card-hdr">
          <span class="tx-rank">#${i + 1}</span>
          <span class="tx-strength ${st.cls}">${st.label}</span>
          <span class="tx-gain">+${s.gain.toFixed(1)} <small>xPts</small></span>
        </div>
        <div class="tx-swap">
          ${playerSide(s.out, 'out')}
          <div class="tx-arrow">→</div>
          ${playerSide(s.in, 'in')}
        </div>
        <div class="tx-foot">
          <span>Price change <b>${cost}</b></span>
          <span>Bank after <b>£${s.bank_after.toFixed(1)}m</b></span>
          ${bench}
        </div>
        ${alts}
      </div>`;
  }

  function squadTable(squad) {
    const rows = [...squad].sort((a, b) => a.xpts - b.xpts).map(p => `
      <tr class="${p.starter ? '' : 'bench'}">
        <td>${esc(p.name)} ${chanceTag(p)}</td>
        <td>${posBadge(p.position)}</td>
        <td>${esc(p.team)}</td>
        <td>£${p.price.toFixed(1)}m</td>
        <td>${p.pred.toFixed(2)}</td>
        <td>${fdrBadge(p.fdr)}</td>
        <td><b>${p.xpts.toFixed(1)}</b></td>
        <td>${p.starter ? 'XI' : 'Bench'}</td>
      </tr>`).join('');
    return `
      <table class="tx-squad">
        <thead><tr><th>Player</th><th>Pos</th><th>Club</th><th>Price</th><th>Pred</th><th>FDR</th><th>xPts</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function loadTransfers() {
    const wrap = document.getElementById('tx-wrap');
    loaded = true;
    wrap.innerHTML = '<div class="spinner"><div class="spin"></div> Analysing your squad…</div>';
    const saved = localStorage.getItem('fplTeamId') || '';
    const url = saved ? `/api/transfers?id=${encodeURIComponent(saved)}` : '/api/transfers';
    try {
      const res = await fetch(url);
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      document.getElementById('txHint').textContent =
        `Bank £${data.bank.toFixed(1)}m · planning for the next ${data.horizon} GWs` +
        (data.next_gameweek ? ` from GW${data.next_gameweek}` : '');

      const cards = data.suggestions.length
        ? data.suggestions.map(suggestionCard).join('')
        : '<p class="text-muted small">No swap beats your current squad by a meaningful margin. Roll the transfer.</p>';
      const fewer = data.suggestions.length && data.suggestions.length < 5
        ? `<p class="tx-fewer">Only ${data.suggestions.length} swap${data.suggestions.length > 1 ? 's' : ''} clear the bar with your current bank. Everything else in your squad is already close to the best option at its price.</p>`
        : '';

      wrap.innerHTML = `
        <div class="tx-method">
          <b>How it works.</b> Every player gets <i>xPts</i> for the next ${data.horizon} GWs:
          ML predicted score × fixture factor (FDR 2 → ×1.15, FDR 4 → ×0.85) × chance of playing.
          A swap's gain is the change in your best starting XI plus captain, so upgrading a bench
          player only counts if he'd start. Replacements must fit your bank, the 3-per-club rule,
          and have played regularly. Prices are current prices; your real selling price may be a little lower.
        </div>
        <div class="tx-list">${cards}</div>
        ${fewer}
        <h3 class="ml-h tx-h">Your squad's outlook (weakest first)</h3>
        <div class="tx-squad-wrap">${squadTable(data.squad)}</div>`;
    } catch (e) {
      loaded = false;
      wrap.innerHTML = `<p class="text-danger small p-3">Failed to load suggestions: ${esc(e.message || e)}</p>`;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('txRefreshBtn').addEventListener('click', loadTransfers);
  });

  window.ensureTransfersLoaded = function () {
    if (!loaded) loadTransfers();
  };
  // Team ID changed in My Squad: refresh if the tab has already been opened
  window.reloadTransfers = function () {
    if (loaded) loadTransfers();
  };
})();
