/**
 * Chart factory module — initialises Chart.js instances from live data objects.
 * Each function accepts a data object and a canvas ID, returns the Chart instance.
 * Used by both index.html (full dashboard) and hud.html (mini card) after live polling.
 */

const GOLD = '#C8A96E', WARN_AMBER = '#C99A52', CLIFF_RED = '#C2554B';
const GRID = 'rgba(255,255,255,0.05)';

export function initMemoryChart(canvasId, samples = [], warnGb = 45, cliffGb = 55) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  const n = samples.length || 60;
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: samples.map((_, i) => i),
      datasets: [
        {
          data: samples, borderColor: GOLD, borderWidth: 1.5,
          pointRadius: 0, tension: 0.3, fill: true,
          backgroundColor: (c) => {
            const g = c.chart.ctx.createLinearGradient(0, 0, 0, c.chart.height);
            g.addColorStop(0, 'rgba(200,169,110,0.22)');
            g.addColorStop(1, 'rgba(200,169,110,0.0)');
            return g;
          },
        },
        { type: 'line', borderColor: WARN_AMBER, borderWidth: 1, borderDash: [5,4], data: new Array(n).fill(warnGb), pointRadius: 0, fill: false, tension: 0 },
        { type: 'line', borderColor: CLIFF_RED,  borderWidth: 1, borderDash: [5,4], data: new Array(n).fill(cliffGb), pointRadius: 0, fill: false, tension: 0 },
      ],
    },
    options: {
      maintainAspectRatio: false, responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { display: false, grid: { display: false } },
        y: { min: 14, max: 60, grid: { color: GRID }, ticks: { stepSize: 10, callback: v => v + 'G' } },
      },
    },
  });
}

export function initPieChart(canvasId, slices = []) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: slices.map(s => s.label),
      datasets: [{ data: slices.map(s => s.gb), backgroundColor: slices.map(s => s.color), borderColor: '#0E0E0E', borderWidth: 2 }],
    },
    options: {
      maintainAspectRatio: false, responsive: true, cutout: '58%',
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => c.label + ' ' + c.parsed + 'GB' } } },
    },
  });
}

export function initSparkline(canvasId, data = [], warnLine = 30, maxY = 75) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map((_, i) => i + 1),
      datasets: [
        { data, borderColor: GOLD, borderWidth: 1.4, pointRadius: 0, tension: 0.25, fill: true, backgroundColor: 'rgba(200,169,110,0.10)' },
        { data: new Array(data.length || 10).fill(warnLine), borderColor: WARN_AMBER, borderWidth: 1, borderDash: [4,3], pointRadius: 0, fill: false },
      ],
    },
    options: {
      maintainAspectRatio: false, responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { display: false, grid: { display: false } },
        y: { min: 0, max: maxY, grid: { color: GRID }, ticks: { maxTicksLimit: 3 } },
      },
    },
  });
}

export function initClassBars(canvasId, stats = []) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  return new Chart(ctx, {
    type: 'bar',
    data: {
      labels: stats.map(s => `${s.model}`),
      datasets: [{ data: stats.map(s => s.avg_rate), backgroundColor: GOLD, borderRadius: 2 }],
    },
    options: {
      maintainAspectRatio: false, responsive: true, indexAxis: 'y',
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: GRID }, ticks: { maxTicksLimit: 4 } },
        y: { grid: { display: false } },
      },
    },
  });
}
