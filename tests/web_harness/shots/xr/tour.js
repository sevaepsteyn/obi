// XR media capture: the fly-around (jsFile argument of readme_media.mjs `video`). Frames the
// relay hub, then drives the REAL navigation: the right thumbstick orbits and zooms about the
// gaze anchor, and A cycles health → signal → utilization → fresnel. Timings are milliseconds.
(() => {
  const d = window.__obiDebug, dev = window.__xrDevice, w = d.world;
  const c = document.querySelector('canvas');
  c.parentElement.style.zIndex = 2147483647;
  c.width = innerWidth * 2; c.height = innerHeight * 2;
  dev.fovy = 75 * Math.PI / 180;
  const s = 0.0006, ry = -70 * Math.PI / 180, q = [-5000, 460, -1850], t = [0, -1.2, -3];
  const rx = q[0] * Math.cos(ry) + q[2] * Math.sin(ry), rz = -q[0] * Math.sin(ry) + q[2] * Math.cos(ry);
  w.scale.setScalar(s); w.rotation.y = ry;
  w.position.set(t[0] - s * rx, t[1] - s * q[1], t[2] - s * rz);
  const th = -22 * Math.PI / 180;
  dev.quaternion.set(Math.sin(th / 2), 0, 0, Math.cos(th / 2));
  for (const h of ['left', 'right']) dev.controllers[h].position.set(h === 'left' ? -0.3 : 0.3, -3, 0.5);
  const R = dev.controllers.right;
  const at = (ms, fn) => setTimeout(fn, ms);
  const stick = (x, y) => R.updateAxes('thumbstick', x, y);
  const tap = (b) => { R.updateButtonValue(b, 1); setTimeout(() => R.updateButtonValue(b, 0), 250); };
  // B twice: terrain shaded → off → wireframe (trim this first 1.5 s when assembling)
  tap('b-button'); at(600, () => tap('b-button'));
  at(2000, () => stick(0.38, 0));
  at(5500, () => tap('a-button'));
  at(8500, () => tap('a-button'));
  at(11500, () => { stick(0, 0); tap('a-button'); });
  at(12500, () => stick(0, -0.42));
  at(14600, () => stick(0, 0));
  return 'tour scheduled';
})()
