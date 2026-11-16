// Headless check of talk.js renderMarkdown (feature 082): HTML-escape-first safety plus the small
// set of markdown constructs obi emits. renderMarkdown is a pure string->string function (no DOM),
// so it runs in plain node exactly like the talk_control state-machine harness.
//
// Usage: node talk_render.mjs <path-to-talk.js>
// Output (last line): a JSON object of rendered HTML strings, one per scenario.
const modulePath = process.argv[2];
const { renderMarkdown, renderReport } = await import(modulePath);

// 089: renderReport scenarios (pure string->string, contracts/turn-report.md).
const reportUsage = {
  input_tokens: 812,
  output_tokens: 310,
  cache_creation_input_tokens: 0,
  cache_read_input_tokens: 11620,
  model_calls: 3,
};
const reportWithTools = {
  model: 'claude-sonnet-5',
  tools: [
    {
      name: 'routeros_query',
      arguments: { device: 'core-gw-1' },
      result: '<script>alert(1)</script> & "quoted"',
      truncated: false,
      error: false,
    },
    { name: 'routeros_query', arguments: {}, result: 'error: unreachable', truncated: false, error: true },
    { name: 'ping', arguments: { target: '172.18.84.1' }, result: 'x'.repeat(40), truncated: true, error: false },
  ],
  usage: reportUsage,
  cost_usd: 0.0192,
};

const out = {
  bold: renderMarkdown('**tamarack-gw** is up for **7 weeks**'),
  escape: renderMarkdown('<img src=x onerror=alert(1)> & "q" \'p\''),
  list: renderMarkdown('- one\n- two'),
  ordered: renderMarkdown('1. first\n2. second'),
  link: renderMarkdown('[docs](https://ex.io) then [bad](javascript:alert(1))'),
  code: renderMarkdown('run `a **b** c` now'),
  plain: renderMarkdown('all links are up 3 of 3'),
  reportTools: renderReport(reportWithTools),
  reportNoTools: renderReport({
    model: 'claude-sonnet-5',
    tools: [],
    usage: reportUsage,
    cost_usd: 0.0192,
  }),
  reportUnknownCost: renderReport({
    model: 'some-local-llm',
    tools: [],
    usage: reportUsage,
    cost_usd: null,
  }),
  reportAbsent: renderReport(null),
};
console.log(JSON.stringify(out));
