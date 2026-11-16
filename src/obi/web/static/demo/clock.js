// Static public demo (feature 136): the frozen demo clock. It is only ever copied into demo
// bundles.
//
// Every widget computes "5 min ago", query windows and tails against the browser clock. The
// demo's data ends at the dataset window's end, months in the past. This installs a Date in
// which "now" is that end plus the real time elapsed since the page loaded. The data looks live
// and keeps ageing naturally while the visitor watches.
//
// It is a Proxy over the real Date, not a subclass:
//   * Date() called without `new` still returns a string;
//   * `instanceof Date` still holds for dates created before or after installation;
//   * Date.parse, Date.UTC and every other static keep the real behaviour.
// Only the no-argument constructor and Date.now() move.

export function installDemoClock(demoNowIso) {
  const RealDate = globalThis.Date;
  const demoNow = RealDate.parse(demoNowIso);
  if (Number.isNaN(demoNow)) throw new Error(`bad demo time: ${demoNowIso}`);
  const offset = demoNow - RealDate.now();
  const now = () => RealDate.now() + offset;

  const DemoDate = new Proxy(RealDate, {
    construct(target, args, newTarget) {
      return args.length === 0
        ? Reflect.construct(target, [now()], newTarget)
        : Reflect.construct(target, args, newTarget);
    },
    apply() {
      return new RealDate(now()).toString();
    },
    get(target, prop, receiver) {
      if (prop === 'now') return now;
      return Reflect.get(target, prop, receiver);
    },
  });
  globalThis.Date = DemoDate;
  return { offsetMs: offset, restore() { globalThis.Date = RealDate; } };
}
