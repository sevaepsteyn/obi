// PING widget (feature 048) — run a ping from a chosen RouterOS device via the web tool bridge.
// A thin wrapper over the shared net-tool form; the work is obi's existing routeros_ping tool.

import { mountNetTool } from '../nettool.js';

export async function mount(host, ctx) {
  return mountNetTool(host, ctx, { tool: 'routeros_ping', verb: 'ping', withCount: true });
}
