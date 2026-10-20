let radarChart = null;
let allPlayers = [];
let selectedMetrics = [];
let thresholdValues = [];   // normalized 0-100 per axis: 0 = metric min (centre), 100 = metric max (edge)
let metricStats = [];        // {min, max, avg, decimals} per metric
let draggingIndex = null;

const HANDLE_RADIUS = 18;   // px — hit-test tolerance for each dot

// ── UNITS ────────────────────────────────────────────────
// Each axis is labelled in its metric's own unit, inferred from the column
// name so new /api/data columns still get a sensible format.
function metricUnit(name) {
  if (/price|gbp/i.test(name)) return { prefix: "£", suffix: "m" };
  if (/per app/i.test(name))   return { prefix: "", suffix: "/app" };
  if (/points|median|^avg$/i.test(name)) return { prefix: "", suffix: " pts" };
  return { prefix: "", suffix: "" };
}

function formatMetric(i, value) {
  const s = metricStats[i];
  return `${s.unit.prefix}${value.toFixed(s.decimals)}${s.unit.suffix}`;
}

// Normalized 0-100 position on axis i → raw metric value
function rawValue(i, pct) {
  const s = metricStats[i];
  return s.min + (pct / 100) * (s.max - s.min);
}

// Draws per-axis value ticks (mid + outer ring) and the current threshold
// next to each draggable dot. Chart.js radar only has one shared radial
// scale, so per-metric units have to be painted by hand.
const axisValuesPlugin = {
  id: "axisValues",
  afterDatasetsDraw(chart) {
    const scale = chart.scales.r;
    const ctx = chart.ctx;
    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    function tag(text, x, y, color, bg, bold) {
      ctx.font = `${bold ? "700" : "500"} ${bold ? 11 : 10}px system-ui, sans-serif`;
      const w = ctx.measureText(text).width + 8;
      ctx.fillStyle = bg;
      ctx.fillRect(x - w / 2, y - 8, w, 16);
      ctx.fillStyle = color;
      ctx.fillText(text, x, y);
    }

    metricStats.forEach((s, i) => {
      [50, 100].forEach((pct) => {
        const pos = scale.getPointPosition(i, (pct / 100) * scale.drawingArea);
        tag(formatMetric(i, rawValue(i, pct)), pos.x, pos.y, "#64748b", "rgba(255,255,255,0.8)", false);
      });
    });
    // Threshold tags sit just outside their dot, along the axis direction,
    // so dots clustered near the centre don't stack their labels.
    metricStats.forEach((s, i) => {
      const pos = scale.getPointPosition(i, (thresholdValues[i] / 100) * scale.drawingArea);
      const edge = scale.getPointPosition(i, scale.drawingArea);
      const dx = edge.x - scale.xCenter, dy = edge.y - scale.yCenter;
      const len = Math.hypot(dx, dy) || 1;
      const off = 24;
      tag(`≥ ${formatMetric(i, rawValue(i, thresholdValues[i]))}`,
          pos.x + (dx / len) * off, pos.y + (dy / len) * off, "#fff", "#2563eb", true);
    });
    ctx.restore();
  },
};

async function fetchFplData() {
  const response = await fetch("/api/data");
  allPlayers = await response.json();
  return allPlayers;
}

document.addEventListener("DOMContentLoaded", async () => {
  const drawBtn = document.getElementById("drawPolygonBtn");
  drawBtn.addEventListener("click", handleDrawPolygon);
  await fetchFplData();
});

async function handleDrawPolygon() {
  selectedMetrics = Array.from(
    document.querySelectorAll(".metric-options input:checked")
  ).map((i) => i.value);

  if (selectedMetrics.length < 3) {
    alert("Please select at least 3 metrics!");
    return;
  }
  if (selectedMetrics.length > 6) {
    alert("Please select at most 6 metrics!");
    return;
  }

  // Per-metric range and mean: each axis spans that metric's own min → max
  metricStats = selectedMetrics.map((m) => {
    const vals = allPlayers.map((p) => parseFloat(p[m])).filter((v) => !isNaN(v));
    const min = vals.length ? Math.min(...vals) : 0;
    let max = vals.length ? Math.max(...vals) : 1;
    if (max === min) max = min + 1;
    const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
    const decimals = vals.every(Number.isInteger) ? 0 : (max - min) < 10 ? 2 : 1;
    return { min, max, avg, decimals, unit: metricUnit(m) };
  });

  // Start thresholds at the per-metric mean, as a 0-100 position along the axis
  thresholdValues = metricStats.map((s) =>
    Math.min(100, Math.max(0, ((s.avg - s.min) / (s.max - s.min)) * 100))
  );

  const ctx = document.getElementById("radarChart").getContext("2d");
  if (radarChart) radarChart.destroy();

  radarChart = new Chart(ctx, {
    type: "radar",
    plugins: [axisValuesPlugin],
    data: {
      labels: selectedMetrics,
      datasets: [{
        label: "Thresholds",
        data: [...thresholdValues],
        fill: true,
        backgroundColor: "rgba(37,99,235,0.18)",
        borderColor: "#2563eb",
        pointBackgroundColor: "#2563eb",
        pointHoverBackgroundColor: "#1d4ed8",
        pointRadius: 8,
        pointHoverRadius: 11,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        title: {
          display: true,
          text: "Drag the dots to filter players",
          color: "#475569",
          font: { size: 12 },
        },
        tooltip: { enabled: false },
      },
      scales: {
        r: {
          min: 0,
          max: 100,
          // Shared 0-100 scale is meaningless across metrics; axisValuesPlugin
          // draws real per-metric values instead.
          ticks: { display: false, stepSize: 25 },
          grid: { color: "rgba(100,116,139,0.25)" },
          angleLines: { color: "rgba(100,116,139,0.4)" },
          pointLabels: {
            font: { size: 11, weight: '600' },
            color: "#1e293b",
            padding: 14,
            // Max is drawn on the outer ring; the centre is each metric's min.
            callback: (label, i) => [label, `from ${formatMetric(i, metricStats[i].min)}`],
          },
        },
      },
      // Disable built-in click/hover element detection — we handle it ourselves
      events: ["mousemove", "mousedown", "mouseup", "mouseleave"],
      onHover: () => {},
      onClick: () => {},
    },
  });

  // ── DRAG INTERACTION ──────────────────────────────────────
  const canvas = radarChart.canvas;

  canvas.addEventListener("mousedown", (event) => {
    const { offsetX: ox, offsetY: oy } = event;
    const scale = radarChart.scales.r;

    for (let i = 0; i < thresholdValues.length; i++) {
      // getPointPosition(index, distanceFromCenter) → {x, y} in canvas pixels
      const pct = thresholdValues[i] / 100;
      const pos = scale.getPointPosition(i, pct * scale.drawingArea);
      if (Math.hypot(ox - pos.x, oy - pos.y) <= HANDLE_RADIUS) {
        draggingIndex = i;
        canvas.style.cursor = "grabbing";
        event.preventDefault();
        break;
      }
    }
  });

  canvas.addEventListener("mousemove", (event) => {
    const { offsetX: ox, offsetY: oy } = event;
    const scale = radarChart.scales.r;

    if (draggingIndex === null) {
      // Update cursor to "grab" when hovering over a handle
      let onHandle = false;
      for (let i = 0; i < thresholdValues.length; i++) {
        const pct = thresholdValues[i] / 100;
        const pos = scale.getPointPosition(i, pct * scale.drawingArea);
        if (Math.hypot(ox - pos.x, oy - pos.y) <= HANDLE_RADIUS) {
          onHandle = true;
          break;
        }
      }
      canvas.style.cursor = onHandle ? "grab" : "default";
      return;
    }

    // Distance from chart centre → new 0-100 value
    const dist = Math.hypot(ox - scale.xCenter, oy - scale.yCenter);
    const newVal = Math.min(100, Math.max(0, (dist / scale.drawingArea) * 100));

    thresholdValues[draggingIndex] = newVal;
    radarChart.data.datasets[0].data = [...thresholdValues];
    radarChart.update("none");
    filterPlayersByThreshold();
  });

  canvas.addEventListener("mouseup", () => {
    draggingIndex = null;
    canvas.style.cursor = "default";
  });

  canvas.addEventListener("mouseleave", () => {
    draggingIndex = null;
    canvas.style.cursor = "default";
  });

  filterPlayersByThreshold();
}

// ── FILTER & RENDER ────────────────────────────────────────
function filterPlayersByThreshold() {
  if (!selectedMetrics.length) return;

  const suggestionsDiv = document.getElementById("suggestion-list");
  suggestionsDiv.innerHTML = "";

  // Denormalize thresholds back to raw metric units before comparing
  const filtered = allPlayers.filter((p) =>
    selectedMetrics.every((m, i) => {
      const val = parseFloat(p[m]);
      return !isNaN(val) && val >= rawValue(i, thresholdValues[i]);
    })
  );

  const top = filtered
    .sort((a, b) => b["Total Points"] - a["Total Points"])
    .slice(0, 10);

  if (top.length === 0) {
    suggestionsDiv.innerHTML = "<p>No players match your current polygon filter.</p>";
    return;
  }

  top.forEach((p) => {
    const div = document.createElement("div");
    div.classList.add("player-card");
    div.innerHTML = `
      <img src="https://resources.premierleague.com/premierleague/photos/players/250x250/p${p.PlayerPhoto || "99999"}.png"
           onerror="this.src='https://cdn-icons-png.flaticon.com/512/149/149071.png'">
      <div>
        <strong>${p.Player}</strong><br>
        <small>${p.Team} | ${p.Position}</small><br>
        <small style="color:#2563eb;">Points: ${p["Total Points"]}</small><br>
        <small class="text-muted">${selectedMetrics
          .map((m, i) => `${m}: ${formatMetric(i, parseFloat(p[m]))}`)
          .join(" · ")}</small>
      </div>`;
    suggestionsDiv.appendChild(div);
  });
}
