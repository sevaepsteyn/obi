// Per-link detail window (feature 058) — one backhaul link's full detail, promoted out of the
// MAP's in-canvas panel into a real workspace window (the 047/051/052 synthetic-window pattern
// applied to a LINK instead of a device).
//
// Opened from a MAP link tap (ctx.openLink) or a LINKS-board row click, keyed by the normalized
// endpoint-identity pair in ctx.params.key (bands.js linkKey). Read-only and device-blind: it
// reads the SAME links.json + terrain.json the MAP polls and filters to this one link — no new
// live route, no new store. Content mirrors the MAP panel: health, endpoints/sites, band,
// frequency, length, signal, capacity/usage, clearance verdict, and the elevation profile.

import { capacityLine, formatRate, linkColor, linkKey, usagePercent } from '../bands.js';
import { fetchJson } from '../kit.js';

const POLL_MS = 5 * 60 * 1000; // the export cadence every surface shares

// 063: HEALTH_COLORS / MAINTENANCE_COLOR / linkColor now live in bands.js (imported above) — one
// palette across the MAP, the 3D scene, and this window. (The clearance PASS/FAIL green/red used
// below is a different vocabulary and stays local.)

function clearanceFlagged(link) {
  return link.clearance_verdict === 'marginal' || link.clearance_verdict === 'obstructed';
}

export async function mount(host, ctx) {
  const key = ctx.params?.key ?? '';

  host.classList.add('linkw');
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';

  const body = document.createElement('div');
  body.className = 'linkw-body';
  host.append(body);

  // State: the polled export + the once-fetched terrain; the resolved link is derived each render.
  let geometry = null;
  let terrain = null;
  let refreshFailing = false;

  function resolve() {
    if (!geometry) return null;
    return (geometry.links ?? []).find((l) => linkKey(l.a, l.b) === key) ?? null;
  }

  function row(label, value) {
    const div = document.createElement('div');
    div.className = 'kit-facts'; // 076: shared fact-pair chrome (was .linkw-body .row)
    const dt = document.createElement('span');
    dt.className = 'k';
    dt.textContent = label;
    const dd = document.createElement('span');
    dd.className = 'v';
    dd.textContent = value;
    div.append(dt, dd);
    return div;
  }

  function message(text) {
    body.replaceChildren();
    const div = document.createElement('div');
    div.className = 'kit-message'; // 076: shared muted message box (was .linkw-message)
    div.textContent = text;
    body.append(div);
  }

  function render() {
    const link = resolve();
    if (!link) {
      // No export yet vs a real absence: both keep the window honest, neither throws.
      message(
        geometry === null
          ? 'no link export yet — run: obi web export'
          : 'this link is not in the current export',
      );
      ctx.setBadge?.(refreshFailing ? 'refresh failing' : null);
      return;
    }
    ctx.setName?.(link.name);

    body.replaceChildren();
    const endpointSite = new Map((geometry.endpoints ?? []).map((e) => [e.name, e.site]));

    const health = document.createElement('div');
    health.className = 'health';
    // Maintenance wins the color but never hides the underlying health (044 US3).
    health.textContent =
      link.maintenance === true ? `maintenance · ${link.health}` : link.health;
    health.style.color = linkColor(link);
    body.append(health);

    body.append(row('A', `${link.a} (${endpointSite.get(link.a) ?? '?'})`));
    body.append(row('B', `${link.b} (${endpointSite.get(link.b) ?? '?'})`));
    if (link.band) body.append(row('band', link.band));
    if (link.frequency_ghz != null) {
      const provenance =
        link.frequency_provenance && link.frequency_provenance !== 'controller'
          ? ` (${link.frequency_provenance})`
          : '';
      body.append(row('frequency', `${link.frequency_ghz} GHz${provenance}`));
    }
    if (link.length_m != null) {
      body.append(row('length', `${(link.length_m / 1000).toFixed(2)} km`));
    }
    if (link.signal_dbm != null) body.append(row('signal', `${link.signal_dbm} dBm`));

    // Capacity + usage: down/up split when known; explicit wording when the controller reports
    // nothing (a dark radio) so absence never reads as zero (044 FR-009).
    if (link.capacity_down_bps != null || link.capacity_up_bps != null) {
      body.append(row(
        'capacity',
        `${formatRate(link.capacity_down_bps) ?? '—'} ↓ / ${formatRate(link.capacity_up_bps) ?? '—'} ↑`,
      ));
      if (link.usage_down_bps != null || link.usage_up_bps != null) {
        const percent = usagePercent(link);
        body.append(row(
          'usage',
          `${formatRate(link.usage_down_bps) ?? '—'} ↓ / ${formatRate(link.usage_up_bps) ?? '—'} ↑` +
          (percent ? ` (${percent})` : ''),
        ));
      }
    } else {
      body.append(row('capacity', 'unavailable'));
    }

    if (link.clearance_verdict != null) {
      const verdict = row(
        'clearance',
        `${link.clearance_verdict} · worst ${link.clearance_margin_m?.toFixed(1)} m ` +
        `at ${Math.round((link.clearance_at_fraction ?? 0) * 100)}% of the path`,
      );
      verdict.classList.add(`verdict-${link.clearance_verdict}`);
      body.append(verdict);
    }

    // Elevation-profile chart: only for a link whose export carries a terrain profile; absence
    // degrades to the verdict fields above, chartless (031 contract).
    const profile = terrain && terrain.profiles ? terrain.profiles[link.id] : null;
    if (profile && Array.isArray(profile.samples_amsl) && profile.samples_amsl.length >= 2) {
      const chart = document.createElement('canvas');
      chart.className = 'profile';
      body.append(chart);
      const caption = document.createElement('div');
      caption.className = 'caption';
      caption.textContent =
        'Path profile: terrain, line of sight, first Fresnel zone; ● = worst clearance.';
      body.append(caption);
      requestAnimationFrame(() => drawProfile(chart, link, profile));
    }

    const status = refreshFailing ? ' · refresh failing' : '';
    ctx.setBadge?.((link.maintenance === true ? 'maintenance' : link.health) + status);
  }

  // The same profile drawing the MAP panel uses (031): terrain silhouette, line of sight, the
  // first-Fresnel envelope (the (D/2, r, r) ellipsoid's 2D section, half-height r·2√(t(1−t))),
  // and the precomputed worst-clearance marker. Every number is an export field — no geodesy.
  function drawProfile(chart, link, profile) {
    const ratio = window.devicePixelRatio || 1;
    const cssWidth = chart.clientWidth || body.clientWidth - 24;
    const cssHeight = 150;
    if (cssWidth <= 0) return;
    chart.width = Math.round(cssWidth * ratio);
    chart.height = Math.round(cssHeight * ratio);
    chart.style.height = `${cssHeight}px`;
    const c = chart.getContext('2d');
    c.setTransform(ratio, 0, 0, ratio, 0, 0);

    const samples = profile.samples_amsl;
    const byName = new Map((geometry.endpoints ?? []).map((e) => [e.name, e]));
    const heightA = byName.get(link.a)?.height_amsl_m;
    const heightB = byName.get(link.b)?.height_amsl_m;
    const radius = link.fresnel_radius_m ?? 0;
    const los = (t) =>
      heightA != null && heightB != null ? heightA + (heightB - heightA) * t : null;
    const envelope = (t) => radius * 2 * Math.sqrt(t * (1 - t));

    let lo = Math.min(...samples);
    let hi = Math.max(...samples);
    if (heightA != null && heightB != null) {
      lo = Math.min(lo, heightA - radius, heightB - radius);
      hi = Math.max(hi, heightA + radius, heightB + radius);
    }
    const pad = Math.max(2, (hi - lo) * 0.12);
    lo -= pad;
    hi += pad;
    const left = 34, right = 6, top = 6, bottom = 16;
    const plotW = cssWidth - left - right;
    const plotH = cssHeight - top - bottom;
    const sx = (t) => left + t * plotW;
    const sy = (amsl) => top + (1 - (amsl - lo) / (hi - lo)) * plotH;

    c.clearRect(0, 0, cssWidth, cssHeight);

    // terrain: filled silhouette
    c.beginPath();
    c.moveTo(sx(0), sy(samples[0]));
    for (let i = 1; i < samples.length; i++) {
      c.lineTo(sx(i / (samples.length - 1)), sy(samples[i]));
    }
    c.lineTo(sx(1), cssHeight - bottom);
    c.lineTo(sx(0), cssHeight - bottom);
    c.closePath();
    c.fillStyle = 'rgba(148, 121, 91, 0.45)';
    c.fill();

    if (heightA != null && heightB != null) {
      // Fresnel envelope: translucent band around the line of sight
      c.beginPath();
      const steps = 48;
      for (let i = 0; i <= steps; i++) {
        const t = i / steps;
        const y = sy(los(t) - envelope(t));
        if (i === 0) c.moveTo(sx(t), y);
        else c.lineTo(sx(t), y);
      }
      for (let i = steps; i >= 0; i--) {
        const t = i / steps;
        c.lineTo(sx(t), sy(los(t) + envelope(t)));
      }
      c.closePath();
      c.fillStyle = 'rgba(96, 165, 250, 0.18)';
      c.fill();

      // line of sight
      c.beginPath();
      c.moveTo(sx(0), sy(los(0)));
      c.lineTo(sx(1), sy(los(1)));
      c.strokeStyle = '#60a5fa';
      c.lineWidth = 1.5;
      c.stroke();
    }

    // worst-clearance marker (precomputed: at_fraction + margin)
    if (link.clearance_at_fraction != null && heightA != null && heightB != null) {
      const t = link.clearance_at_fraction;
      const y = sy(los(t) - envelope(t));
      c.beginPath();
      c.arc(sx(t), y, 4, 0, Math.PI * 2);
      c.fillStyle = clearanceFlagged(link) ? '#ef4444' : '#22c55e';
      c.fill();
      if (link.clearance_margin_m != null) {
        c.font = '11px system-ui, sans-serif';
        c.fillStyle = '#e6edf3';
        c.textAlign = t > 0.5 ? 'right' : 'left';
        c.fillText(`${link.clearance_margin_m.toFixed(1)} m`, sx(t) + (t > 0.5 ? -8 : 8), y - 8);
      }
    }

    // axes: elevation ticks + span label
    c.font = '10px system-ui, sans-serif';
    c.fillStyle = '#9fb0c0';
    c.textAlign = 'left';
    c.fillText(`${Math.round(hi)} m`, 2, top + 8);
    c.fillText(`${Math.round(lo)} m`, 2, cssHeight - bottom);
    if (link.length_m != null) {
      c.textAlign = 'center';
      c.fillText(`${(link.length_m / 1000).toFixed(2)} km`, left + plotW / 2, cssHeight - 4);
    }
  }

  async function pollOnce() {
    try {
      geometry = await fetchJson('links.json', { cache: 'no-cache' });
      refreshFailing = false;
    } catch (err) {
      refreshFailing = true;
      if (geometry === null) { message(`could not load the link export (${err})`); return; }
    }
    render();
  }

  async function fetchTerrainOnce() {
    try {
      const response = await fetch('terrain.json', { cache: 'no-cache' });
      if (response.ok) terrain = await response.json();
    } catch (err) {
      terrain = null; // chartless degrade — the verdict fields still render
    }
    render();
  }

  message('loading…');
  await pollOnce();
  await fetchTerrainOnce(); // once per window (never in the poll — the 031 static-artifact rule)
  const pollTimer = setInterval(pollOnce, POLL_MS);

  return {
    unmount() {
      clearInterval(pollTimer);
    },
  };
}
