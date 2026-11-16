// obi window manager (feature 037) — free-floating windows, zero dependencies.
//
// Same input idiom as viewer2d.js: Pointer Events + setPointerCapture, one code path for
// mouse and touch (the Quest browser is a touch pointer). The WM owns geometry, stacking,
// and state transitions (data-model.md: WindowState); the shell owns what goes INSIDE a
// window (widget mount/unmount) and when to persist (onMutate fires after every mutation).
//
// State model: x/y/w/h always describe the CURRENT on-screen rect. `restoreGeometry` is
// the pre-snap / pre-maximize rect to return to; it is non-null iff state is snapped/max.
// Minimize hides the window without touching geometry, so restore-from-dock is lossless.

const TITLE_H = 28; // keep in sync with .win-title (clamping keeps this strip reachable)
const MIN_VISIBLE = 80; // px of title bar that must stay inside the workspace
const EDGE_SNAP = 16; // pointer-to-edge distance that arms a half snap
const CORNER_SNAP = 96; // pointer-to-corner box that arms a quarter snap

const ZONES = ['left', 'right', 'top', 'bottom', 'tl', 'tr', 'bl', 'br'];

export function createWindowManager(root, dock, { onMutate, onClose }) {
  const wins = new Map(); // widget id -> win
  const zOrder = []; // bottom -> top
  let preview = null; // the snap preview overlay while dragging

  function mutated() {
    onMutate?.();
  }

  function zoneRect(zone) {
    const W = root.clientWidth;
    const H = root.clientHeight;
    const half = { w: Math.floor(W / 2), h: H };
    const halfV = { w: W, h: Math.floor(H / 2) };
    const quarter = { w: Math.floor(W / 2), h: Math.floor(H / 2) };
    switch (zone) {
      case 'left': return { x: 0, y: 0, ...half };
      case 'right': return { x: W - half.w, y: 0, ...half };
      case 'top': return { x: 0, y: 0, ...halfV };
      case 'bottom': return { x: 0, y: H - halfV.h, ...halfV };
      case 'tl': return { x: 0, y: 0, ...quarter };
      case 'tr': return { x: W - quarter.w, y: 0, ...quarter };
      case 'bl': return { x: 0, y: H - quarter.h, ...quarter };
      case 'br': return { x: W - quarter.w, y: H - quarter.h, ...quarter };
      default: return null;
    }
  }

  function zoneAt(px, py) {
    // Pointer position (workspace-relative) -> snap zone. Corners win over edges.
    const W = root.clientWidth;
    const H = root.clientHeight;
    const nearLeft = px <= EDGE_SNAP;
    const nearRight = px >= W - EDGE_SNAP;
    const nearTopEdge = py <= EDGE_SNAP;
    const nearBottomEdge = py >= H - EDGE_SNAP;
    // Corners use the taller CORNER_SNAP band (along the left/right edges) so a quarter is
    // easy to hit; they win over the top/bottom half-snaps that live in the middle strip.
    const inCornerTop = py <= CORNER_SNAP;
    const inCornerBottom = py >= H - CORNER_SNAP;
    if (nearLeft && inCornerTop) return 'tl';
    if (nearLeft && inCornerBottom) return 'bl';
    if (nearRight && inCornerTop) return 'tr';
    if (nearRight && inCornerBottom) return 'br';
    if (nearLeft) return 'left';
    if (nearRight) return 'right';
    if (nearTopEdge) return 'top';
    if (nearBottomEdge) return 'bottom';
    return null;
  }

  function showPreview(zone) {
    if (!zone) {
      hidePreview();
      return;
    }
    if (!preview) {
      preview = document.createElement('div');
      preview.id = 'snap-preview';
      root.appendChild(preview);
    }
    const rect = zoneRect(zone);
    preview.style.left = `${rect.x}px`;
    preview.style.top = `${rect.y}px`;
    preview.style.width = `${rect.w}px`;
    preview.style.height = `${rect.h}px`;
  }

  function hidePreview() {
    preview?.remove();
    preview = null;
  }

  function clamp(win) {
    const W = root.clientWidth;
    const H = root.clientHeight;
    win.w = Math.max(win.def.minSize[0], Math.min(win.w, Math.max(win.def.minSize[0], W)));
    win.h = Math.max(win.def.minSize[1], Math.min(win.h, Math.max(win.def.minSize[1], H)));
    win.x = Math.min(Math.max(win.x, MIN_VISIBLE - win.w), Math.max(0, W - MIN_VISIBLE));
    win.y = Math.min(Math.max(win.y, 0), Math.max(0, H - TITLE_H));
  }

  function apply(win, { resized = true } = {}) {
    win.el.style.left = `${win.x}px`;
    win.el.style.top = `${win.y}px`;
    win.el.style.width = `${win.w}px`;
    win.el.style.height = `${win.h}px`;
    if (resized) win.handle?.onResize?.();
  }

  function restack() {
    zOrder.forEach((win, index) => {
      win.el.style.zIndex = String(10 + index);
      win.el.classList.toggle('focused', index === zOrder.length - 1);
    });
  }

  function focus(win) {
    const index = zOrder.indexOf(win);
    if (index !== -1 && index !== zOrder.length - 1) {
      zOrder.splice(index, 1);
      zOrder.push(win);
      restack();
      mutated();
    } else if (index === zOrder.length - 1) {
      restack();
    }
  }

  // --- per-window behaviors ------------------------------------------------------------------

  function startDrag(win, event) {
    if (win.state === 'max') return; // maximized windows don't drag (restore first)
    const rootBox = root.getBoundingClientRect();
    const grabX = event.clientX - rootBox.left - win.x;
    const grabY = event.clientY - rootBox.top - win.y;
    let unsnapped = win.state !== 'snapped';
    win.titleEl.setPointerCapture(event.pointerId);

    function onMove(move) {
      const px = move.clientX - rootBox.left;
      const py = move.clientY - rootBox.top;
      if (!unsnapped) {
        // Dragging a snapped window away un-snaps it back to its remembered size,
        // re-anchored under the pointer so the grab doesn't jump.
        unsnapped = true;
        const back = win.restoreGeometry ?? { w: win.def.defaultSize[0], h: win.def.defaultSize[1] };
        win.w = back.w;
        win.h = back.h;
        win.state = 'normal';
        win.snapZone = null;
        win.restoreGeometry = null;
      }
      win.x = px - Math.min(grabX, win.w - MIN_VISIBLE);
      win.y = py - Math.min(grabY, TITLE_H);
      apply(win, { resized: false });
      showPreview(zoneAt(px, py));
    }

    function onUp(up) {
      win.titleEl.removeEventListener('pointermove', onMove);
      win.titleEl.removeEventListener('pointerup', onUp);
      win.titleEl.removeEventListener('pointercancel', onUp);
      const zone = up.type === 'pointerup'
        ? zoneAt(up.clientX - rootBox.left, up.clientY - rootBox.top)
        : null;
      hidePreview();
      if (zone) {
        snapTo(win, zone);
      } else {
        clamp(win);
        apply(win, { resized: false });
        mutated();
      }
    }

    win.titleEl.addEventListener('pointermove', onMove);
    win.titleEl.addEventListener('pointerup', onUp);
    win.titleEl.addEventListener('pointercancel', onUp);
  }

  function snapTo(win, zone) {
    if (win.state === 'normal') {
      win.restoreGeometry = { x: win.x, y: win.y, w: win.w, h: win.h };
    }
    const rect = zoneRect(zone);
    win.state = 'snapped';
    win.snapZone = zone;
    Object.assign(win, rect);
    apply(win);
    mutated();
  }

  function startResize(win, direction, event) {
    if (win.state === 'max') return;
    const handleEl = event.currentTarget;
    handleEl.setPointerCapture(event.pointerId);
    const start = { px: event.clientX, py: event.clientY, x: win.x, y: win.y, w: win.w, h: win.h };
    if (win.state === 'snapped') {
      // Resizing a snapped window makes it a normal window at its current rect.
      win.state = 'normal';
      win.snapZone = null;
      win.restoreGeometry = null;
    }
    let raf = 0;

    function onMove(move) {
      const dx = move.clientX - start.px;
      const dy = move.clientY - start.py;
      const [minW, minH] = win.def.minSize;
      if (direction.includes('e')) win.w = Math.max(minW, start.w + dx);
      if (direction.includes('s')) win.h = Math.max(minH, start.h + dy);
      if (direction.includes('w')) {
        win.w = Math.max(minW, start.w - dx);
        win.x = start.x + (start.w - win.w);
      }
      if (direction.includes('n')) {
        win.h = Math.max(minH, start.h - dy);
        win.y = start.y + (start.h - win.h);
      }
      if (!raf) {
        raf = requestAnimationFrame(() => {
          raf = 0;
          apply(win);
        });
      }
    }

    function onUp() {
      handleEl.removeEventListener('pointermove', onMove);
      handleEl.removeEventListener('pointerup', onUp);
      handleEl.removeEventListener('pointercancel', onUp);
      if (raf) cancelAnimationFrame(raf);
      clamp(win);
      apply(win);
      mutated();
    }

    handleEl.addEventListener('pointermove', onMove);
    handleEl.addEventListener('pointerup', onUp);
    handleEl.addEventListener('pointercancel', onUp);
  }

  function minimize(win) {
    if (win.state === 'min') return;
    win.stateBeforeMin = win.state;
    win.state = 'min';
    win.el.style.display = 'none';
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.innerHTML = `<span class="mn"></span> <span class="ti"></span>`;
    chip.querySelector('.mn').textContent = win.def.mnemonic;
    chip.querySelector('.ti').textContent = win.def.title;
    chip.addEventListener('click', () => restoreFromMin(win));
    win.dockChip = chip;
    dock.appendChild(chip);
    mutated();
  }

  function restoreFromMin(win) {
    if (win.state !== 'min') return;
    win.state = win.stateBeforeMin ?? (win.snapZone ? 'snapped' : win.restoreGeometry ? 'max' : 'normal');
    win.stateBeforeMin = null;
    win.dockChip?.remove();
    win.dockChip = null;
    win.el.style.display = '';
    apply(win);
    focus(win);
    mutated();
  }

  function toggleMaximize(win) {
    if (win.state === 'max') {
      const back = win.beforeMax;
      win.beforeMax = null;
      if (back?.state === 'snapped' && back.snapZone) {
        win.restoreGeometry = back.restoreGeometry;
        snapTo(win, back.snapZone); // re-snapped at the CURRENT viewport size
        return;
      }
      win.state = 'normal';
      win.snapZone = null;
      win.restoreGeometry = null;
      if (back) Object.assign(win, { x: back.x, y: back.y, w: back.w, h: back.h });
      clamp(win);
      apply(win);
    } else {
      win.beforeMax = {
        state: win.state,
        snapZone: win.snapZone,
        restoreGeometry: win.restoreGeometry,
        x: win.x, y: win.y, w: win.w, h: win.h,
      };
      win.state = 'max';
      win.snapZone = null;
      win.restoreGeometry = { x: win.beforeMax.x, y: win.beforeMax.y, w: win.beforeMax.w, h: win.beforeMax.h };
      Object.assign(win, { x: 0, y: 0, w: root.clientWidth, h: root.clientHeight });
      apply(win);
    }
    mutated();
  }

  function close(win) {
    onClose?.(win); // shell unmounts the widget first
    win.dockChip?.remove();
    const index = zOrder.indexOf(win);
    if (index !== -1) zOrder.splice(index, 1);
    wins.delete(win.def.id);
    win.el.remove();
    restack();
    mutated();
  }

  // --- window construction ---------------------------------------------------------------------

  function create(def, saved = null) {
    const el = document.createElement('section');
    el.className = 'win';
    el.innerHTML = `
      <header class="win-title">
        <span class="mn"></span>
        <span class="name"><span class="base"></span><span class="suffix"></span></span>
        <span class="badge"></span>
        <span class="win-actions">
          <button type="button" class="a-min" title="minimize">–</button>
          <button type="button" class="a-max" title="maximize / restore">□</button>
          <button type="button" class="a-close" title="close">✕</button>
        </span>
      </header>
      <div class="win-body"></div>`;
    for (const direction of ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) {
      const handle = document.createElement('div');
      handle.className = `rs rs-${direction}`;
      handle.dataset.dir = direction;
      el.appendChild(handle);
    }
    el.querySelector('.mn').textContent = def.mnemonic;
    el.querySelector('.name .base').textContent = def.title;

    const win = {
      def,
      el,
      titleEl: el.querySelector('.win-title'),
      body: el.querySelector('.win-body'),
      handle: null, // the widget handle; the shell sets it after mount()
      x: 0, y: 0, w: def.defaultSize[0], h: def.defaultSize[1],
      state: 'normal',
      snapZone: null,
      restoreGeometry: null,
      stateBeforeMin: null,
      beforeMax: null,
      dockChip: null,
      focus: () => focus(win),
      minimize: () => minimize(win),
      restoreFromMin: () => restoreFromMin(win),
      toggleMaximize: () => toggleMaximize(win),
      close: () => close(win),
      setSuffix(text) {
        el.querySelector('.name .suffix').textContent = text ? ` — ${text}` : '';
      },
      setName(text) {
        // 047: a dynamic window (a router opened by address) can rename itself once it learns
        // its identity from the live read.
        if (text) el.querySelector('.name .base').textContent = text;
      },
      setBadge(text) {
        el.querySelector('.badge').textContent = text ?? '';
      },
    };

    // A fresh window opens cascaded from the top-left; a restored one gets its saved rect.
    if (saved) {
      Object.assign(win, {
        x: saved.x, y: saved.y, w: saved.w, h: saved.h,
        state: saved.state === 'min' ? 'normal' : saved.state, // minimize applied below
        snapZone: saved.snapZone ?? null,
        restoreGeometry: saved.restore ?? null,
      });
      if (win.state === 'snapped' && win.snapZone) Object.assign(win, zoneRect(win.snapZone));
      if (win.state === 'max') Object.assign(win, { x: 0, y: 0, w: root.clientWidth, h: root.clientHeight });
    } else {
      const cascade = 28 * (wins.size % 8);
      win.x = 24 + cascade;
      win.y = 24 + cascade;
    }
    clamp(win);

    win.titleEl.addEventListener('pointerdown', (event) => {
      if (event.target.closest('.win-actions')) return;
      focus(win);
      startDrag(win, event);
    });
    win.titleEl.addEventListener('dblclick', (event) => {
      if (event.target.closest('.win-actions')) return;
      toggleMaximize(win);
    });
    el.addEventListener('pointerdown', () => focus(win), { capture: true });
    el.querySelector('.a-min').addEventListener('click', () => minimize(win));
    el.querySelector('.a-max').addEventListener('click', () => toggleMaximize(win));
    el.querySelector('.a-close').addEventListener('click', () => close(win));
    for (const handle of el.querySelectorAll('.rs')) {
      handle.addEventListener('pointerdown', (event) => {
        focus(win);
        startResize(win, handle.dataset.dir, event);
      });
    }

    root.appendChild(el);
    wins.set(def.id, win);
    zOrder.push(win);
    restack();
    apply(win, { resized: false });
    if (saved?.state === 'min') minimize(win);
    // Opening a window is itself a mutation. Without this, a window the operator opens and only
    // READS — a DASH or HEALTH board, never dragged, never buried — is in no saved layout: it
    // opens on top, so a click on it hits the `index === zOrder.length - 1` branch of focus()
    // that restacks without notifying, and the next reload restores the workspace as it was
    // before that window existed. The shell suppresses this during its boot restore.
    mutated();
    return win;
  }

  // --- workspace-level operations ----------------------------------------------------------------

  function clampAll() {
    for (const win of wins.values()) {
      if (win.state === 'max') {
        Object.assign(win, { x: 0, y: 0, w: root.clientWidth, h: root.clientHeight });
      } else if (win.state === 'snapped' && win.snapZone) {
        Object.assign(win, zoneRect(win.snapZone));
      } else {
        clamp(win);
      }
      if (win.state !== 'min') apply(win);
    }
  }

  function serialize() {
    // z-order = array order, bottom -> top (contracts/workspace-urls.md). 076: ephemeral windows
    // (snapshot: <uuid>) are never written — they exist only for this session and vanish on reload.
    // 077: this filter is correct FOR PERSISTENCE and is exactly why `list()` below does not reuse
    // it — the switcher must show what is OPEN, not what will survive a reload.
    return zOrder
      .filter((win) => !win.def.ephemeral)
      .map((win) => ({
        widget: win.def.id,
        x: win.x, y: win.y, w: win.w, h: win.h,
        state: win.state,
        snapZone: win.snapZone,
        restore: win.restoreGeometry,
      }));
  }

  // --- the switcher seam (077, contracts/window-switcher.md) --------------------------------------
  //
  // The WM owns the switcher's SEMANTICS; the shell only renders. That split is deliberate: it
  // makes the shell unable to express the two ways this goes wrong (see `activate` and the source
  // note on `list`). The shell gets a read-only projection and one verb.

  function list() {
    // Source is zOrder, NOT serialize(): minimize() leaves a window in zOrder and only close()
    // splices it out, so zOrder is exactly "the open windows" — ephemeral ones included, which is
    // the point (a chat or snapshot window is open and switchable even though it is never saved).
    // Reversed, so the top-most window leads. Titles are read here, not cached, so a window that
    // renamed itself (047) shows its current name.
    return zOrder
      .map((win) => ({
        id: win.def.id,
        mnemonic: win.def.mnemonic,
        title: win.def.title,
        minimized: win.state === 'min',
      }))
      .reverse();
  }

  function activate(id) {
    const win = wins.get(id);
    if (!win) return; // the entry's window closed between the menu opening and the click
    // A minimized window is at display:none, so focus() alone would reorder z-index and change
    // NOTHING the operator can see. Restoring is not an optimization here — it is the whole
    // interaction. restoreFromMin already focuses (and restores state, geometry, and the dock
    // chip), so this is a two-branch dispatch, never a re-implementation.
    if (win.state === 'min') restoreFromMin(win);
    else focus(win);
  }

  return {
    create,
    get: (id) => wins.get(id) ?? null,
    focus,
    clampAll,
    serialize,
    list,
    activate,
    get count() { return wins.size; },
  };
}
