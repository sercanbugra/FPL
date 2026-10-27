// ── MINI LEAGUE TAB ────────────────────────────────────────
// Renders /api/mini-league as an animated report: hero stats, an
// auto-playing weekly "story" of awards, a bar-chart race of cumulative
// points, a league-position bump chart, a GW heatmap and season records.
(function () {
  const PALETTE = [
    '#2563eb', '#dc2626', '#16a34a', '#d97706', '#7c3aed', '#0891b2',
    '#db2777', '#65a30d', '#ea580c', '#4f46e5', '#0d9488', '#9333ea',
  ];
  const CHIP_NAMES = { wildcard: 'Wildcard', freehit: 'Free Hit', bboost: 'Bench Boost',
                       '3xc': 'Triple Captain', manager: 'Assistant Manager' };
  const CHIP_SHORT = { wildcard: 'WC', freehit: 'FH', bboost: 'BB', '3xc': 'TC', manager: 'AM' };
  const STORY_MS = 6000;
  const RACE_STEP_MS = 1300;
  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let ML = null;          // API payload
  let gwIdx = 0;          // selected GW index into ML.gameweeks
  let myEntry = null;
  let colorOf = {};
  let raceTimer = null;
  let bumpChart = null;
  let loaded = false;

  // ── helpers ──
  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function week(entry, idx) { return ML.weeks[String(entry)][idx]; }

  function countUp(el, target, { prefix = '', suffix = '', decimals = 0, ms = 1200 } = {}) {
    const fmt = v => `${prefix}${v.toFixed(decimals)}${suffix}`;
    if (REDUCED || typeof target !== 'number') { el.textContent = typeof target === 'number' ? fmt(target) : target; return; }
    const start = performance.now();
    function frame(now) {
      const t = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = fmt(target * eased);
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  // Award values are numbers in different units; map each award to its format
  function valueFormat(a) {
    switch (a.key) {
      case 'value':    return { prefix: '£', suffix: 'm', decimals: 1 };
      case 'steady':   return { prefix: '±', suffix: '', decimals: 1 };
      case 'climber':  return { prefix: '▲ ', suffix: '' };
      case 'faller':   return { prefix: '▼ ', suffix: '' };
      case 'wins':     return { prefix: '', suffix: '×' };
      default:         return { prefix: '', suffix: ' pts' };
    }
  }

  function animateValue(el, a) {
    if (a.key === 'chip') { el.textContent = CHIP_NAMES[a.value] || a.value; return; }
    countUp(el, a.value, valueFormat(a));
  }

  function managerChip(entry) {
    const m = ML.managers.find(x => x.entry === entry);
    const me = entry === myEntry ? ' me' : '';
    return `<span class="ml-dot" style="background:${colorOf[entry]}"></span><span class="ml-team${me}">${esc(m ? m.team : '')}</span>`;
  }

  // Run fn once when el scrolls into view (so animations play where the user is looking)
  function onVisible(el, fn) {
    if (!('IntersectionObserver' in window)) { fn(); return; }
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) { io.disconnect(); fn(); } });
    }, { threshold: 0.25 });
    io.observe(el);
  }

  // ── HERO ──
  function renderHero() {
    const gw = ML.gameweeks[gwIdx];
    const rows = ML.managers.map(m => ({ m, w: week(m.entry, gwIdx) })).filter(r => r.w.points !== null);
    const leader = [...rows].sort((a, b) => a.w.league_rank - b.w.league_rank)[0];
    const top = [...rows].sort((a, b) => b.w.points - a.w.points)[0];
    const avg = rows.reduce((s, r) => s + r.w.points, 0) / (rows.length || 1);
    const mine = rows.find(r => r.m.entry === myEntry);

    let mineHtml = '';
    if (mine) {
      const prev = gwIdx > 0 ? week(myEntry, gwIdx - 1).league_rank : mine.w.league_rank;
      const diff = prev - mine.w.league_rank;
      const arrow = diff > 0 ? `<span class="ml-up">▲${diff}</span>` : diff < 0 ? `<span class="ml-down">▼${-diff}</span>` : '<span class="ml-same">●</span>';
      mineHtml = `
        <div class="ml-tile">
          <div class="ml-tile-label">Your position</div>
          <div class="ml-tile-value">#<span data-count="${mine.w.league_rank}">0</span> ${arrow}</div>
          <div class="ml-tile-sub">${esc(mine.m.team)} · ${mine.w.points} pts</div>
        </div>`;
    }

    document.getElementById('ml-hero-stats').innerHTML = `
      <div class="ml-tile">
        <div class="ml-tile-label">Leader after GW${gw}</div>
        <div class="ml-tile-value"><span data-count="${leader.w.total}">0</span><small> pts</small></div>
        <div class="ml-tile-sub">${esc(leader.m.team)}</div>
      </div>
      <div class="ml-tile">
        <div class="ml-tile-label">Top score GW${gw}</div>
        <div class="ml-tile-value"><span data-count="${top.w.points}">0</span><small> pts</small></div>
        <div class="ml-tile-sub">${esc(top.m.team)}</div>
      </div>
      <div class="ml-tile">
        <div class="ml-tile-label">League average</div>
        <div class="ml-tile-value"><span data-count="${avg.toFixed(1)}" data-dec="1">0</span><small> pts</small></div>
        <div class="ml-tile-sub">${rows.length} managers</div>
      </div>
      ${mineHtml}`;
    document.querySelectorAll('#ml-hero-stats [data-count]').forEach(el =>
      countUp(el, parseFloat(el.dataset.count), { decimals: parseInt(el.dataset.dec || '0', 10) }));

    document.getElementById('ml-gw-label').textContent = `Gameweek ${gw}`;
    document.getElementById('ml-gw-prev').disabled = gwIdx <= 0;
    document.getElementById('ml-gw-next').disabled = gwIdx >= ML.gameweeks.length - 1;
  }

  // ── WEEKLY STORY (auto-playing slides) ──
  let storyIdx = 0;
  let storySlides = [];

  function podiumSlide(gw) {
    const top3 = ML.managers
      .map(m => ({ m, w: week(m.entry, gwIdx) }))
      .filter(r => r.w.points !== null)
      .sort((a, b) => b.w.points - a.w.points)
      .slice(0, 3);
    const max = top3.length ? top3[0].w.points : 1;
    // Visual podium order: 2nd, 1st, 3rd
    const order = [top3[1], top3[0], top3[2]].filter(Boolean);
    const medals = { 0: '🥇', 1: '🥈', 2: '🥉' };
    const cols = order.map(r => {
      const place = top3.indexOf(r);
      const h = 35 + 55 * (r.w.points / max);
      return `
        <div class="ml-podium-col">
          <div class="ml-podium-team">${esc(r.m.team)}</div>
          <div class="ml-podium-bar place-${place + 1}" style="--h:${h}%">
            <span class="ml-podium-medal">${medals[place]}</span>
            <span class="ml-podium-pts">${r.w.points}</span>
          </div>
        </div>`;
    }).join('');
    return `
      <div class="ml-slide theme-podium">
        <div class="ml-slide-kicker">Gameweek ${gw} podium</div>
        <div class="ml-podium">${cols}</div>
      </div>`;
  }

  function awardSlide(a) {
    const m = ML.managers.find(x => x.entry === a.entry);
    return `
      <div class="ml-slide theme-${a.key}${a.entry === myEntry ? ' is-me' : ''}">
        <div class="ml-slide-emoji">${a.emoji}</div>
        <div class="ml-slide-kicker">${esc(a.title)}</div>
        <div class="ml-slide-value" data-award='${esc(JSON.stringify(a))}'></div>
        <div class="ml-slide-team">${esc(a.team)}${a.entry === myEntry ? ' <span class="ml-you">YOU</span>' : ''}</div>
        <div class="ml-slide-player">${esc(m ? m.player : a.player)}</div>
        <div class="ml-slide-detail">${esc(a.detail)}</div>
      </div>`;
  }

  function renderStory() {
    const gw = ML.gameweeks[gwIdx];
    const awards = ML.awards[String(gw)] || [];
    storySlides = [podiumSlide(gw), ...awards.map(awardSlide)];
    document.getElementById('ml-story-bars').innerHTML =
      storySlides.map((_, i) => `<div class="ml-story-bar" data-i="${i}"><span></span></div>`).join('');
    document.querySelectorAll('#ml-story-bars .ml-story-bar').forEach(b =>
      b.addEventListener('click', () => showSlide(parseInt(b.dataset.i, 10))));
    showSlide(0);
  }

  function showSlide(i) {
    storyIdx = (i + storySlides.length) % storySlides.length;
    const stage = document.getElementById('ml-story-stage');
    stage.innerHTML = storySlides[storyIdx];
    const valEl = stage.querySelector('.ml-slide-value');
    if (valEl) animateValue(valEl, JSON.parse(valEl.dataset.award));

    document.querySelectorAll('#ml-story-bars .ml-story-bar').forEach((b, j) => {
      b.classList.toggle('done', j < storyIdx);
      b.classList.remove('active');
      const fill = b.querySelector('span');
      fill.style.animation = 'none';
      void fill.offsetWidth; // restart CSS animation
      fill.style.animation = '';
      if (j === storyIdx) {
        b.classList.add('active');
        fill.style.animationDuration = `${STORY_MS}ms`;
        fill.onanimationend = () => {
          // Only auto-advance while the tab is actually on screen
          if (document.getElementById('pane-league').classList.contains('active')) showSlide(storyIdx + 1);
          else showSlide(storyIdx);
        };
      } else {
        fill.onanimationend = null;
      }
    });
  }

  // ── BAR CHART RACE ──
  const ROW_H = 34;

  function renderRaceSkeleton() {
    const wrap = document.getElementById('ml-race-rows');
    wrap.style.height = `${ML.managers.length * ROW_H}px`;
    wrap.innerHTML = ML.managers.map(m => `
      <div class="ml-race-row${m.entry === myEntry ? ' me' : ''}" data-entry="${m.entry}">
        <div class="ml-race-name">${esc(m.team)}</div>
        <div class="ml-race-track">
          <div class="ml-race-bar" style="background:${colorOf[m.entry]}"></div>
          <span class="ml-race-val">0</span>
        </div>
      </div>`).join('');
  }

  function raceStep(idx) {
    const gw = ML.gameweeks[idx];
    const rows = ML.managers.map(m => ({ m, w: week(m.entry, idx) }))
      .sort((a, b) => a.w.league_rank - b.w.league_rank);
    const max = Math.max(1, ...rows.map(r => r.w.total));
    rows.forEach((r, pos) => {
      const el = document.querySelector(`.ml-race-row[data-entry="${r.m.entry}"]`);
      el.style.transform = `translateY(${pos * ROW_H}px)`;
      el.querySelector('.ml-race-bar').style.width = `${Math.max(2, (r.w.total / max) * 100)}%`;
      const val = el.querySelector('.ml-race-val');
      const from = parseFloat(val.textContent) || 0;
      if (REDUCED) { val.textContent = r.w.total; return; }
      const start = performance.now();
      (function tick(now) {
        const t = Math.min(1, (now - start) / (RACE_STEP_MS * 0.8));
        val.textContent = Math.round(from + (r.w.total - from) * t);
        if (t < 1) requestAnimationFrame(tick);
      })(start);
    });
    document.getElementById('ml-race-gw').textContent = `GW${gw}`;
    document.getElementById('ml-race-slider').value = idx;
  }

  function playRace(fromStart) {
    clearInterval(raceTimer);
    const slider = document.getElementById('ml-race-slider');
    let idx = fromStart ? 0 : parseInt(slider.value, 10);
    if (idx >= ML.gameweeks.length - 1) idx = 0;
    raceStep(idx);
    document.getElementById('ml-race-play').textContent = '⏸ Pause';
    raceTimer = setInterval(() => {
      idx += 1;
      if (idx >= ML.gameweeks.length) { stopRace(); return; }
      raceStep(idx);
    }, RACE_STEP_MS);
  }

  function stopRace() {
    clearInterval(raceTimer);
    raceTimer = null;
    document.getElementById('ml-race-play').textContent = '▶ Play';
  }

  // ── BUMP CHART (league position per GW) ──
  function renderBump() {
    const ctx = document.getElementById('ml-bump-chart');
    if (bumpChart) bumpChart.destroy();
    const n = ML.managers.length;
    bumpChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: ML.gameweeks.map(g => `GW${g}`),
        datasets: ML.managers.map(m => ({
          label: m.team,
          data: ML.weeks[String(m.entry)].map(w => w.league_rank),
          borderColor: colorOf[m.entry],
          backgroundColor: colorOf[m.entry],
          borderWidth: m.entry === myEntry ? 5 : 2.5,
          pointRadius: m.entry === myEntry ? 5 : 3,
          tension: 0.35,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: REDUCED ? false : {
          duration: 700,
          delay: c => (c.type === 'data' && c.mode === 'default') ? c.dataIndex * 220 + c.datasetIndex * 25 : 0,
        },
        interaction: { mode: 'nearest', intersect: false },
        scales: {
          y: { reverse: true, min: 1, max: n, ticks: { stepSize: 1, callback: v => `#${v}` },
               grid: { color: 'rgba(100,116,139,0.15)' } },
          x: { grid: { display: false } },
        },
        plugins: {
          legend: { position: window.innerWidth < 768 ? 'bottom' : 'right',
                    labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: { label: c => `${c.dataset.label}: #${c.parsed.y}` } },
        },
      },
    });
  }

  // ── HEATMAP ──
  function heatColor(t) {
    // t in [0,1]: red → amber → green
    const hue = 4 + t * 128;
    return `hsl(${hue}, 70%, ${88 - t * 10}%)`;
  }

  function renderHeatmap() {
    const gws = ML.gameweeks;
    const colStats = gws.map((_, i) => {
      const pts = ML.managers.map(m => week(m.entry, i).points).filter(p => p !== null);
      return { min: Math.min(...pts), max: Math.max(...pts) };
    });
    const last = gws.length - 1;
    const rows = [...ML.managers].sort((a, b) => week(a.entry, last).league_rank - week(b.entry, last).league_rank);
    const head = `<tr><th>#</th><th class="ml-hm-team">Team</th>${gws.map((g, i) =>
      `<th class="${i === gwIdx ? 'sel' : ''}">GW${g}</th>`).join('')}<th>Total</th></tr>`;
    const body = rows.map((m, r) => `
      <tr class="${m.entry === myEntry ? 'me' : ''}">
        <td class="ml-hm-rank">${week(m.entry, last).league_rank}</td>
        <td class="ml-hm-team">${managerChip(m.entry)}</td>
        ${gws.map((g, i) => {
          const w = week(m.entry, i);
          if (w.points === null) return '<td class="ml-hm-cell empty">–</td>';
          const s = colStats[i];
          const t = s.max === s.min ? 0.5 : (w.points - s.min) / (s.max - s.min);
          const chip = w.chip ? `<span class="ml-hm-chip">${CHIP_SHORT[w.chip] || w.chip}</span>` : '';
          const tip = `GW${g}: ${w.points} pts · #${w.league_rank} in league` +
            (w.captain ? ` · C: ${w.captain.name} (${w.captain.points})` : '') +
            (w.hit ? ` · hit -${w.hit}` : '') + (w.bench ? ` · bench ${w.bench}` : '');
          return `<td class="ml-hm-cell${i === gwIdx ? ' sel' : ''}" title="${esc(tip)}"
                      style="background:${heatColor(t)};animation-delay:${(r * gws.length + i) * 12}ms">${w.points}${chip}</td>`;
        }).join('')}
        <td class="ml-hm-total">${week(m.entry, last).total}</td>
      </tr>`).join('');
    document.getElementById('ml-heatmap').innerHTML = `<table class="ml-hm-table"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  // ── CHIPS TABLE ──
  function renderChips() {
    const wrap = document.getElementById('ml-chips');
    const chips = ML.chips || { slots: [], rows: [] };
    if (!chips.slots.length) { wrap.innerHTML = '<p class="text-muted small">No chip data.</p>'; return; }

    // Group consecutive slots of the same chip under one header (one per season half)
    const groups = [];
    chips.slots.forEach(s => {
      const g = groups[groups.length - 1];
      if (g && g.name === s.name) g.span += 1; else groups.push({ name: s.name, label: s.label, span: 1 });
    });
    const last = ML.gameweeks.length - 1;
    const byEntry = new Map(chips.rows.map(r => [r.entry, r]));
    const rows = [...ML.managers].sort((a, b) => week(a.entry, last).league_rank - week(b.entry, last).league_rank);

    function cell(c, s, delay) {
      const style = `animation-delay:${delay}ms`;
      if (c.status === 'used') {
        const pts = c.points === null || c.points === undefined ? '' : ` · ${c.points} pts`;
        return `<td><span class="ml-chip-pill chip-${s.name}" style="${style}">GW${c.gw}${pts}</span></td>`;
      }
      const text = { available: 'Available', upcoming: `Opens GW${s.start}`, expired: 'Expired' }[c.status];
      return `<td><span class="ml-chip-state ${c.status}" style="${style}">${text}</span></td>`;
    }

    wrap.innerHTML = `
      <table class="ml-chip-table">
        <thead>
          <tr>
            <th rowspan="2" class="ml-hm-team">Team</th>
            ${groups.map(g => `<th colspan="${g.span}" class="ml-chip-head chip-${g.name}">${esc(g.label)}</th>`).join('')}
            <th rowspan="2">Used</th>
          </tr>
          <tr>${chips.slots.map(s => `<th class="ml-chip-half">GW${s.start}–${s.stop}</th>`).join('')}</tr>
        </thead>
        <tbody>
          ${rows.map((m, r) => {
            const row = byEntry.get(m.entry);
            if (!row) return '';
            return `
              <tr class="${m.entry === myEntry ? 'me' : ''}">
                <td class="ml-hm-team">${managerChip(m.entry)}</td>
                ${row.cells.map((c, i) => cell(c, chips.slots[i], (r * row.cells.length + i) * 15)).join('')}
                <td class="ml-chip-used">${row.used}<small>/${chips.slots.length}</small></td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>`;
  }

  // ── SEASON RECORDS ──
  function renderRecords() {
    const grid = document.getElementById('ml-records');
    grid.innerHTML = ML.records.map((a, i) => `
      <div class="ml-record theme-${a.key}${a.entry === myEntry ? ' is-me' : ''}" style="animation-delay:${i * 90}ms">
        <div class="ml-record-emoji">${a.emoji}</div>
        <div class="ml-record-title">${esc(a.title)}</div>
        <div class="ml-record-value" data-i="${i}"></div>
        <div class="ml-record-team">${managerChip(a.entry)}</div>
        <div class="ml-record-detail">${esc(a.detail)}</div>
      </div>`).join('');
    onVisible(grid, () => {
      grid.classList.add('in');
      grid.querySelectorAll('.ml-record-value').forEach(el => animateValue(el, ML.records[parseInt(el.dataset.i, 10)]));
    });
  }

  // ── GW SELECTION ──
  function selectGw(idx) {
    gwIdx = Math.max(0, Math.min(idx, ML.gameweeks.length - 1));
    renderHero();
    renderStory();
    renderHeatmap();
  }

  // ── LOAD ──
  async function loadMiniLeague() {
    const root = document.getElementById('ml-root');
    const status = document.getElementById('ml-status');
    loaded = true;
    status.innerHTML = '<div class="spinner"><div class="spin"></div> Fetching league data… (first load can take a few seconds)</div>';
    root.hidden = true;
    try {
      const res = await fetch('/api/mini-league');
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      if (!data.gameweeks.length || !data.managers.length) {
        status.innerHTML = '<p class="text-muted small p-3">No gameweeks played yet — check back after GW1.</p>';
        return;
      }
      ML = data;
      const saved = parseInt(localStorage.getItem('fplTeamId') || '', 10);
      myEntry = ML.managers.some(m => m.entry === saved) ? saved : data.my_entry;
      colorOf = {};
      ML.managers.forEach((m, i) => { colorOf[m.entry] = PALETTE[i % PALETTE.length]; });

      document.getElementById('ml-league-name').textContent = ML.league.name;
      status.innerHTML = '';
      root.hidden = false;

      gwIdx = ML.gameweeks.length - 1;
      selectGw(gwIdx);

      renderRaceSkeleton();
      const slider = document.getElementById('ml-race-slider');
      slider.max = ML.gameweeks.length - 1;
      raceStep(0);
      onVisible(document.getElementById('ml-race'), () => playRace(true));
      onVisible(document.getElementById('ml-bump-wrap'), renderBump);
      renderChips();
      renderRecords();
    } catch (e) {
      loaded = false;
      status.innerHTML = `<p class="text-danger small p-3">Failed to load mini-league: ${esc(e.message || e)}</p>`;
    }
  }

  function bindControls() {
    document.getElementById('ml-gw-prev').addEventListener('click', () => selectGw(gwIdx - 1));
    document.getElementById('ml-gw-next').addEventListener('click', () => selectGw(gwIdx + 1));
    document.getElementById('ml-story-prev').addEventListener('click', () => showSlide(storyIdx - 1));
    document.getElementById('ml-story-next').addEventListener('click', () => showSlide(storyIdx + 1));
    document.getElementById('ml-race-play').addEventListener('click', () => (raceTimer ? stopRace() : playRace(false)));
    document.getElementById('ml-race-slider').addEventListener('input', e => { stopRace(); raceStep(parseInt(e.target.value, 10)); });
  }

  document.addEventListener('DOMContentLoaded', bindControls);

  window.ensureMiniLeagueLoaded = function () {
    if (!loaded) loadMiniLeague();
  };
})();
