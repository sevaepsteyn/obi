// XR media capture: frame one still. Runs inside the emulated session (jsFile argument of
// readme_media.mjs `shot`); parameters come from the URL hash, e.g.
//   '?w=xr#qx=-5000&qy=460&qz=-1850&s=0.0009&yaw=-75&pitch=-24&ty=-1.2&tz=-3&hands=0'
//   q*     scene point (world-local metres: x east, y up, z south) to put in front of the viewer
//   t*     where it goes, relative to the headset (metres; the reference space origin is the eye)
//   s/yaw  world scale and yaw in degrees — the same transform the thumbsticks drive
//   pitch  head pitch in degrees; fov vertical field of view in degrees
//   mode / terrain   presses of A (view mode) / B (terrain off → wire → shaded), the real buttons
//   hands=0          park the controllers out of view (no aim rays)
(async () => {
  const P = Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
  const n = (k, d) => (P[k] === undefined ? d : Number(P[k]));
  const d = window.__obiDebug, dev = window.__xrDevice, w = d.world;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const c = document.querySelector('canvas');
  c.parentElement.style.zIndex = 2147483647;
  c.width = innerWidth * 2; c.height = innerHeight * 2;
  dev.fovy = n('fov', 75) * Math.PI / 180;
  const s = n('s', 0.0003), ry = n('yaw', 0) * Math.PI / 180;
  const q = [n('qx', -8000), n('qy', 300), n('qz', -4000)], t = [n('tx', 0), n('ty', -0.9), n('tz', -3)];
  const rx = q[0] * Math.cos(ry) + q[2] * Math.sin(ry), rz = -q[0] * Math.sin(ry) + q[2] * Math.cos(ry);
  w.scale.setScalar(s); w.rotation.y = ry;
  w.position.set(t[0] - s * rx, t[1] - s * q[1], t[2] - s * rz);
  const th = n('pitch', -22) * Math.PI / 180;
  dev.quaternion.set(Math.sin(th / 2), 0, 0, Math.cos(th / 2));
  const press = async (hand, b, times) => { for (let i = 0; i < times; i++) { dev.controllers[hand].updateButtonValue(b, 1); await sleep(250); dev.controllers[hand].updateButtonValue(b, 0); await sleep(250); } };
  await press('right', 'a-button', n('mode', 0));
  await press('right', 'b-button', n('terrain', 0));
  if (P.hands === '0') { for (const h of ['left', 'right']) dev.controllers[h].position.set(h === 'left' ? -0.3 : 0.3, -3, 0.5); }
  return JSON.stringify({ mode: d.settings.mode, hash: location.hash });
})()
