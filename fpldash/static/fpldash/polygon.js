let radarChart = null;
let allPlayers = [];
let selectedMetrics = [];
let thresholdValues = [];   // normalized 0-100 per axis
let metricStats = [];        // {max, avg} per metric
let draggingIndex = null;

const HANDLE_RADIUS = 18;   // px — hit-test tolerance for each dot

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

  // Per-metric max and mean (used for normalization and initial thresholds)
  metricStats = selectedMetrics.map((m) => {
    const vals = allPlayers.map((p) => parseFloat(p[m])).filter((v) => !isNaN(v));
    const max = vals.length ? Math.max(...vals) : 1;
    const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
    return { max: max || 1, avg };
  });

  // Start thresholds at the per-metric mean, expressed as a 0-100 percentage
  thresholdValues = metricStats.map((s) => Math.min(100, (s.avg / s.max) * 100));

  const ctx = document.getElementById("radarChart").getContext("2d");
  if (radarChart) radarChart.destroy();

  radarChart = new Chart(ctx, {
    type: "radar",
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
          ticks: {
            display: true,
            stepSize: 25,
            callback: (v) => v + "%",
            color: "#475569",
            backdropColor: "rgba(255,255,255,0.75)",
          },
          grid: { color: "rgba(100,116,139,0.25)" },
          angleLines: { color: "rgba(100,116,139,0.4)" },
          pointLabels: { font: { size: 11, weight: '600' }, color: "#1e293b" },
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
      const threshold = (thresholdValues[i] / 100) * metricStats[i].max;
      return !isNaN(val) && val >= threshold;
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
        <small style="color:#2563eb;">Points: ${p["Total Points"]}</small>
      </div>`;
    suggestionsDiv.appendChild(div);
  });
}
