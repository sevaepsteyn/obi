// Frozen demo clock harness (feature 136, T015).
// Usage: node demo_clock.mjs <path-to-demo/clock.js>
// Output (last line): JSON of observations taken before and after installation.
const { installDemoClock } = await import(process.argv[2]);
const before = new Date(1000);
const realNow = Date.now();
const clock = installDemoClock('2025-06-05T15:59:59-07:00');
const demoTarget = Date.parse('2025-06-05T15:59:59-07:00');
const out = {
  nowDelta: Math.abs(Date.now() - demoTarget),
  newDateDelta: Math.abs(new Date().getTime() - demoTarget),
  explicitUnaffected: new Date(0).getTime() === 0 && new Date(2020, 0, 1).getFullYear() === 2020,
  parseWorks: Date.parse('1970-01-01T00:00:01Z') === 1000,
  utcWorks: Date.UTC(1970, 0, 1, 0, 0, 2) === 2000,
  instanceofOld: before instanceof Date,
  instanceofNew: new Date() instanceof Date,
  callReturnsString: typeof Date() === 'string' && Date().includes('2025'),
  isoInDemoYear: new Date().toISOString().startsWith('2025-06-05'),
  realWasLater: realNow > demoTarget,
};
clock.restore();
out.restored = Math.abs(Date.now() - realNow) < 5000;
console.log(JSON.stringify(out));
