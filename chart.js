// obi chart primitive (feature 041) — a canvas line chart for bucketed time series.
//
// Widget-agnostic (the kit rule): callers hand it series of {label, buckets:[{start, avg,
// n}]} and a unit; it owns pixels. Dataviz discipline applied: categorical series colors
// come from a validated fixed-order dark-surface palette (never cycled, assigned to the
// entity by first appearance so refreshes/filters never repaint a survivor); gap buckets
// (n === 0) BREAK the line — gaps are data; 2px lines over a recessive grid; one y axis
// from zero; a legend always present for >= 2 series; text wears the theme's text tokens,
// never the series hue; hover = nearest-bucket crosshair + a monospace readout row.

// The validated dark-mode categorical order (dataviz reference palette; slots 1-6).
const SERIES_COLORS = ['#3987e5', '#199e70', '#c98500', '#008300', '#9085e9', '#e66767'];
const MAX_SERIES = SERIES_COLORS.length; // beyond this: fold into a "+N more" note

const GRID = 'rgba(139, 147, 163, 0.18)'; // --muted at grid opacity — recessive
const TEXT = '#8b93a3'; // --muted: axis + readout text (text tokens, never series color)
const CROSSHAIR = 'rgba(216, 222, 233, 0.35)';

function formatValue(value, unit) {
  if (value == null) return '—';
  const abs = Math.abs(value);
  let scaled = value;
  let prefix = '';
  if (abs >= 1e9) { scaled = value / 1e9; prefix = 'G'; }
  else if (abs >= 1e6) { scaled = value / 1e6; prefix = 'M'; }
  else if (abs >= 1e3) { scaled = value / 1e3; prefix = 'k'; }
  const digits = Math.abs(scaled) >= 100 ? 0 : Math.abs(scaled) >= 10 ? 1 : 2;
  return `${scaled.toFixed(digits)} ${prefix}${unit || ''}`.trimEnd();
}

export function createChart(host) {
  const canvas = document.createElement('canvas');
  canvas.className = 'chart-canvas';
  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  const readout = document.createElement('div');
  readout.className = 'chart-readout';
  host.append(canvas, legend, readout);
  const ctx = canvas.getContext('2d');

  const colorBySeries = new Map(); // label -> palette slot: color follows the entity
  let current = { series: [], unit: '' };
  let hoverIndex = null;
  let plotLeft = 56; // measured each draw from the widest y label; hover math reuses it

  function colorFor(label) {
    if (!colorBySeries.has(label)) {
      colorBySeries.set(label, SERIES_COLORS[colorBySeries.size % SERIES_COLORS.length]);
    }
    return colorBySeries.get(label);
  }

  function layout() {
    const ratio = window.devicePixelRatio || 1;
    const width = host.clientWidth;
    const height = Math.max(80, host.clientHeight - legend.offsetHeight - readout.offsetHeight);
    canvas.width = Math.max(1, Math.round(width * ratio));
    canvas.height = Math.max(1, Math.round(height * ratio));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { width, height };
  }

  function draw() {
    const { width, height } = layout();
    ctx.clearRect(0, 0, width, height);
    const shown = current.series.slice(0, MAX_SERIES);
    if (!shown.length || !shown[0].buckets.length) return;

    let maxY = 0;
    for (const s of shown) {
      for (const b of s.buckets) if (b.n > 0 && b.avg != null) maxY = Math.max(maxY, b.avg);
    }
    if (maxY <= 0) maxY = 1;
    maxY *= 1.08; // headroom so peaks don't kiss the frame

    // The left margin is measured, not fixed: y labels ("104.43 Mbits/s") must never clip.
    ctx.font = '10px ui-monospace, Menlo, monospace';
    const tickLabels = [0, 0.5, 1].map((t) => formatValue(maxY * t, current.unit));
    const labelW = Math.max(...tickLabels.map((l) => ctx.measureText(l).width));
    const margin = { left: Math.ceil(labelW) + 12, right: 8, top: 8, bottom: 18 };
    plotLeft = margin.left;
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    if (plotW < 20 || plotH < 20) return;

    const count = shown[0].buckets.length;
    const sx = (i) => margin.left + (count <= 1 ? plotW / 2 : (i / (count - 1)) * plotW);
    const sy = (v) => margin.top + plotH - (v / maxY) * plotH;

    // Recessive grid + y ticks (3 lines), monospace muted text.
    ctx.fillStyle = TEXT;
    ctx.strokeStyle = GRID;
    ctx.lineWidth = 1;
    [0, 0.5, 1].forEach((t, i) => {
      const y = margin.top + plotH - t * plotH;
      ctx.beginPath();
      ctx.moveTo(margin.left, y);
      ctx.lineTo(width - margin.right, y);
      ctx.stroke();
      ctx.textAlign = 'right';
      ctx.fillText(tickLabels[i], margin.left - 6, y + 3);
    });
    // x labels: window start / middle / end (bucket starts are ISO strings).
    const first = shown[0].buckets[0].start;
    const last = shown[0].buckets[count - 1].start;
    ctx.textAlign = 'left';
    ctx.fillText(first.slice(11, 16), margin.left, height - 5);
    if (count > 2) {
      ctx.textAlign = 'center';
      ctx.fillText(shown[0].buckets[count >> 1].start.slice(11, 16),
        margin.left + plotW / 2, height - 5);
    }
    ctx.textAlign = 'right';
    ctx.fillText(last.slice(11, 16), width - margin.right, height - 5);

    // A single series also shows its within-bucket min–max as a translucent band (the avg
    // line alone hides short peaks that a coarser bucket averaged away).
    if (shown.length === 1) {
      const s = shown[0];
      ctx.fillStyle = `${colorFor(s.label)}33`;
      let run = [];
      const flush = () => {
        if (run.length > 1) {
          ctx.beginPath();
          run.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
          for (let i = run.length - 1; i >= 0; i--) ctx.lineTo(run[i][0], run[i][2]);
          ctx.closePath();
          ctx.fill();
        }
        run = [];
      };
      s.buckets.forEach((b, i) => {
        if (b.n > 0 && b.min != null && b.max != null) {
          run.push([sx(i), sy(Math.min(b.max, maxY)), sy(b.min)]);
        } else flush();
      });
      flush();
    }

    // Series lines: 2px, gap buckets break the path (a gap is data, never interpolated).
    for (const s of shown) {
      ctx.strokeStyle = colorFor(s.label);
      ctx.lineWidth = 2;
      ctx.beginPath();
      let pen = false;
      s.buckets.forEach((b, i) => {
        if (b.n > 0 && b.avg != null) {
          const x = sx(i);
          const y = sy(b.avg);
          if (pen) ctx.lineTo(x, y);
          else ctx.moveTo(x, y);
          pen = true;
        } else {
          pen = false;
        }
      });
      ctx.stroke();
    }

    // Hover crosshair at the nearest bucket.
    if (hoverIndex != null && hoverIndex >= 0 && hoverIndex < count) {
      const x = sx(hoverIndex);
      ctx.strokeStyle = CROSSHAIR;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, margin.top);
      ctx.lineTo(x, margin.top + plotH);
      ctx.stroke();
    }
  }

  function renderLegend() {
    legend.replaceChildren();
    const shown = current.series.slice(0, MAX_SERIES);
    if (shown.length >= 2 || current.series.length > MAX_SERIES) {
      for (const s of shown) {
        const item = document.createElement('span');
        item.className = 'chart-legend-item';
        const swatch = document.createElement('span');
        swatch.className = 'chart-swatch';
        swatch.style.background = colorFor(s.label);
        const name = document.createElement('span');
        name.textContent = s.label; // identity text in text tokens; the swatch carries hue
        item.append(swatch, name);
        legend.appendChild(item);
      }
      if (current.series.length > MAX_SERIES) {
        const more = document.createElement('span');
        more.className = 'chart-legend-item chart-more';
        more.textContent = `+${current.series.length - MAX_SERIES} more — narrow by interface`;
        legend.appendChild(more);
      }
    }
  }

  function renderReadout() {
    const shown = current.series.slice(0, MAX_SERIES);
    if (hoverIndex == null || !shown.length || !shown[0].buckets[hoverIndex]) {
      readout.textContent = '';
      return;
    }
    const bucket = shown[0].buckets[hoverIndex];
    const parts = [`${bucket.start.slice(5, 16).replace('T', ' ')}`];
    for (const s of shown) {
      const b = s.buckets[hoverIndex];
      parts.push(`${s.label}: ${b && b.n > 0 ? formatValue(b.avg, current.unit) : 'gap'}`);
    }
    readout.textContent = parts.join('  ·  ');
  }

  canvas.addEventListener('pointermove', (event) => {
    const count = current.series[0]?.buckets.length ?? 0;
    if (!count) return;
    const plotW = host.clientWidth - plotLeft - 8;
    const fraction = Math.min(1, Math.max(0, (event.offsetX - plotLeft) / Math.max(1, plotW)));
    hoverIndex = Math.round(fraction * (count - 1));
    draw();
    renderReadout();
  });
  canvas.addEventListener('pointerleave', () => {
    hoverIndex = null;
    draw();
    renderReadout();
  });

  const observer = new ResizeObserver(() => draw());
  observer.observe(host);

  return {
    render(seriesList, { unit = '' } = {}) {
      current = { series: seriesList, unit };
      hoverIndex = null;
      renderLegend();
      draw();
      renderReadout();
    },
    destroy() {
      observer.disconnect();
      canvas.remove();
      legend.remove();
      readout.remove();
    },
  };
}
