// TRACE widget (feature 048) — run a traceroute from a chosen RouterOS device via the tool bridge.
// A thin wrapper over the shared net-tool form; the work is obi's existing routeros_traceroute tool
// (no count field — the traceroute bounds come from [routeros.live], not the operator).

import { mountNetTool } from '../nettool.js';

export async function mount(host, ctx) {
  return mountNetTool(host, ctx, { tool: 'routeros_traceroute', verb: 'trace', withCount: false });
}
