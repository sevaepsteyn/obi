// XR media capture: install an emulated Quest 3 over the browser's own WebXR, so the immersive
// viewer runs in headless Chrome. Append this to Meta's IWER build (MIT, not vendored) and pass
// the result as OBI_INIT — see ../README.md "XR media". Mono rendering: one flat view, not two eyes.
;(() => {
  if (window.top !== window) return;
  const dev = new IWER.XRDevice(IWER.metaQuest3, { stereoEnabled: false });
  dev.installRuntime({ forceInstall: true }); // Chrome ships a native navigator.xr with no device
  dev.position.set(0, 1.7, 0);
  window.__xrDevice = dev;
})();
