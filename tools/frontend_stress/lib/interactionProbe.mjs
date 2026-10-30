// In-page measurement code for interaction-audit.mjs. Everything in
// installInteractionProbe runs inside the browser (Playwright serialises the
// function), so it must stay self-contained. The probe only measures; the
// pass/fail rules live in interactionChecks.mjs where they can be unit tested.

export function installInteractionProbe() {
  if (window.__interactionProbe) return;

  // Anything a user can point at, type into, or that explains itself on hover.
  const INTERACTIVE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=tab]", "[role=combobox]", "[role=option]", "[role=slider]",
    "[role=switch]", "[role=checkbox]", "[role=radio]", "[role=menuitem]", "[role=menuitemradio]",
    "[role=menuitemcheckbox]", "[role=treeitem]", "[tabindex]:not([tabindex='-1'])",
    "[contenteditable='']", "[contenteditable=true]", "[data-tip]", "[title]",
    "label:has(input:not([type=hidden]))", "audio[controls]", "video[controls]",
  ].join(",");
  // Controls proper (the boundary when searching for a full-text affordance).
  const CONTROL = "a[href],button,input,select,textarea,summary,label,[role=button],[role=link],[role=tab],[role=option],[role=combobox]";
  const POPUP_ROLES = new Set(["tooltip", "listbox", "menu", "dialog", "alertdialog"]);
  const POPUP_CLASS = /(^|[\s_-])(tooltip|popover|menu|dropdown)([\s_-]|$)/i;
  const TEXT_INPUT = /^(INPUT|TEXTAREA|SELECT)$/;
  const FOCUS_PROPS = [
    "outline-style", "outline-width", "outline-color", "outline-offset", "box-shadow",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "background-color", "color", "text-decoration-line",
  ];

  const ids = new WeakMap();
  const refs = new Map();
  let nextId = 1;
  const idOf = (element) => {
    let id = ids.get(element);
    if (!id) {
      id = nextId++;
      ids.set(element, id);
      refs.set(id, new WeakRef(element));
    }
    return id;
  };
  const byId = (id) => refs.get(id)?.deref() ?? null;

  const rectOf = (element) => {
    const box = element.getBoundingClientRect();
    return { x: box.left, y: box.top, w: box.width, h: box.height };
  };
  const round = (rect) => rect && ({ x: Math.round(rect.x * 10) / 10, y: Math.round(rect.y * 10) / 10, w: Math.round(rect.w * 10) / 10, h: Math.round(rect.h * 10) / 10 });

  const rendered = (element) => (element.checkVisibility
    ? element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
    : element.getClientRects().length > 0);

  const visuallyHidden = (element, rect = rectOf(element)) => {
    if (rect.w <= 1.5 || rect.h <= 1.5) return true;
    const style = getComputedStyle(element);
    return /rect\(0(px)?,? 0(px)?,? 0(px)?,? 0(px)?\)/.test(style.clip) || style.clipPath === "inset(50%)";
  };

  const isInert = (element) => Boolean(element.closest("[inert]"));

  const accessibleName = (element) => {
    const labelled = element.getAttribute("aria-labelledby");
    const fromIds = labelled ? labelled.split(/\s+/).map((id) => document.getElementById(id)?.textContent ?? "").join(" ") : "";
    const value = element.getAttribute("aria-label") || fromIds || element.textContent
      || element.getAttribute("title") || element.getAttribute("placeholder") || element.value || "";
    return String(value).replace(/\s+/g, " ").trim();
  };

  const describe = (element) => {
    if (!element) return "(none)";
    if (element === document.body || element === document.documentElement) return element.tagName.toLowerCase();
    const id = element.id ? `#${element.id}` : "";
    const classes = typeof element.className === "string" && element.className.trim()
      ? `.${element.className.trim().split(/\s+/).slice(0, 3).join(".")}` : "";
    const role = element.getAttribute("role") ? `[role=${element.getAttribute("role")}]` : "";
    const name = accessibleName(element).slice(0, 60);
    return `${element.tagName.toLowerCase()}${id}${classes}${role}${name ? ` "${name}"` : ""}`;
  };

  // The element whose overflow box clips `element`, honouring positioned escapes:
  // absolute boxes escape non-positioned ancestors and fixed boxes escape all
  // but transformed ones.
  const createsContainingBlock = (style) => style.transform !== "none" || style.filter !== "none"
    || style.perspective !== "none" || /paint|layout|strict|content/.test(style.contain)
    || /transform|filter/.test(style.willChange);
  const clipParent = (element) => {
    const position = getComputedStyle(element).position;
    if (position === "fixed") {
      for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
        if (createsContainingBlock(getComputedStyle(ancestor))) return ancestor;
      }
      return null;
    }
    if (position === "absolute") {
      for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        if (style.position !== "static" || createsContainingBlock(style)) return ancestor;
      }
      return null;
    }
    return element.parentElement;
  };

  const clipChain = (element) => {
    const chain = [];
    for (let ancestor = clipParent(element); ancestor && ancestor !== document.documentElement; ancestor = clipParent(ancestor)) {
      const style = getComputedStyle(ancestor);
      if (style.overflowX === "visible" && style.overflowY === "visible") continue;
      const box = ancestor.getBoundingClientRect();
      chain.push({
        id: idOf(ancestor),
        desc: describe(ancestor),
        overflowX: style.overflowX,
        overflowY: style.overflowY,
        box: { x: box.left + ancestor.clientLeft, y: box.top + ancestor.clientTop, w: ancestor.clientWidth, h: ancestor.clientHeight },
        scrollLeft: ancestor.scrollLeft,
        scrollTop: ancestor.scrollTop,
        maxScrollLeft: Math.max(0, ancestor.scrollWidth - ancestor.clientWidth),
        maxScrollTop: Math.max(0, ancestor.scrollHeight - ancestor.clientHeight),
      });
    }
    return chain;
  };

  const viewportBox = () => ({ x: 0, y: 0, w: document.documentElement.clientWidth, h: document.documentElement.clientHeight });
  const intersect = (a, b) => {
    const x = Math.max(a.x, b.x);
    const y = Math.max(a.y, b.y);
    const right = Math.min(a.x + a.w, b.x + b.w);
    const bottom = Math.min(a.y + a.h, b.y + b.h);
    return right > x && bottom > y ? { x, y, w: right - x, h: bottom - y } : null;
  };
  const visibleRect = (rect, chain) => chain.reduce((visible, clip) => (visible ? intersect(visible, clip.box) : null), intersect(rect, viewportBox()));

  const overlayOf = (element) => {
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      const position = getComputedStyle(node).position;
      if (position === "fixed" || position === "sticky") return node;
    }
    return null;
  };
  const scrollerOf = (element) => {
    for (let node = element.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (/(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) return node;
      if (node === document.documentElement) break;
    }
    const root = document.scrollingElement;
    return root && root.scrollHeight > root.clientHeight + 1 ? root : null;
  };

  // Hit-test the centre and inset corners of the visible part of `element`.
  const coverage = (element, rect, chain) => {
    const visible = visibleRect(rect, chain);
    if (!visible || visible.w < 2 || visible.h < 2) return null;
    const inset = Math.min(3, visible.w / 4, visible.h / 4);
    const points = [
      ["center", visible.x + visible.w / 2, visible.y + visible.h / 2],
      ["top-left", visible.x + inset, visible.y + inset],
      ["top-right", visible.x + visible.w - inset, visible.y + inset],
      ["bottom-left", visible.x + inset, visible.y + visible.h - inset],
      ["bottom-right", visible.x + visible.w - inset, visible.y + visible.h - inset],
    ];
    const pointerEvents = getComputedStyle(element).pointerEvents;
    const hits = [];
    for (const [name, x, y] of points) {
      const hit = document.elementFromPoint(x, y);
      if (!hit || hit === element || element.contains(hit)) continue;
      // An ancestor is "hit" where the element's own shape (radius) or its
      // pointer-events leave the point to it; nothing covers the element there.
      if (hit.contains(element) || pointerEvents === "none") continue;
      // A label forwards to its control, and a control can sit inside its label.
      if (element.tagName === "LABEL" && (element.control === hit || hit.closest("label") === element)) continue;
      const overlay = overlayOf(hit);
      const coveringOverlay = overlay && !overlay.contains(element) ? overlay : null;
      const popup = hit.closest(".tooltip-bubble,[role=listbox],[role=menu],[role=dialog],[role=alertdialog],[role=tooltip]");
      hits.push({
        point: name,
        hitId: idOf(hit),
        hitDesc: describe(hit),
        overlayDesc: coveringOverlay ? describe(coveringOverlay) : null,
        overlayRect: coveringOverlay ? round(rectOf(coveringOverlay)) : null,
        inPopup: Boolean(popup && !popup.contains(element)),
      });
    }
    if (!hits.length) return null;
    const scroller = scrollerOf(element);
    return {
      hits,
      remainingUp: scroller ? scroller.scrollTop : 0,
      remainingDown: scroller ? Math.max(0, scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop) : 0,
    };
  };

  // Scroll areas whose top or bottom edge sits under a fixed bar (a player bar,
  // a banner) spanning most of their width. Whatever scrolls to that edge ends
  // up under the bar unless the area reserves padding for it. Popups, dialogs,
  // backdrops and narrow transient layers (a skip link, a toast) are not bars.
  const scrollAreasUnderBars = () => {
    const bars = [...document.querySelectorAll("body *")].filter((element) => {
      const style = getComputedStyle(element);
      if (style.position !== "fixed" || style.visibility === "hidden" || style.display === "none") return false;
      if (isPopup(element, style) || element.closest("[aria-hidden='true'], [role=dialog], [role=alertdialog]")) return false;
      if (/backdrop/i.test(typeof element.className === "string" ? element.className : "")) return false;
      const rect = rectOf(element);
      return rect.w > 0 && rect.h > 0 && intersect(rect, viewportBox());
    });
    if (!bars.length) return [];
    const areas = [...document.querySelectorAll("body *")].filter((element) => {
      const style = getComputedStyle(element);
      return /(auto|scroll|overlay)/.test(style.overflowY) && element.clientHeight > 0 && !isInert(element) && element.checkVisibility();
    });
    const found = [];
    for (const area of areas) {
      const outer = area.getBoundingClientRect();
      const port = { x: outer.left + area.clientLeft, y: outer.top + area.clientTop, w: area.clientWidth, h: area.clientHeight };
      const style = getComputedStyle(area);
      for (const bar of bars) {
        if (bar.contains(area) || area.contains(bar)) continue;
        const overlap = intersect(port, rectOf(bar));
        if (!overlap || overlap.w < port.w / 2) continue;
        const edge = overlap.y + overlap.h >= port.y + port.h - 1 ? "bottom" : overlap.y <= port.y + 1 ? "top" : null;
        if (!edge) continue;
        found.push({
          desc: describe(area),
          barDesc: describe(bar),
          edge,
          overlap: round(overlap),
          reserved: parseFloat(edge === "bottom" ? style.paddingBottom : style.paddingTop) || 0,
        });
      }
    }
    return found;
  };

  const isPopup = (element, style) => {
    const role = element.getAttribute("role");
    if (role && POPUP_ROLES.has(role)) return true;
    if (element.hasAttribute("popover")) return true;
    const className = typeof element.className === "string" ? element.className : "";
    return (style.position === "absolute" || style.position === "fixed") && POPUP_CLASS.test(className);
  };

  const fullText = (element) => (element.textContent ?? "").replace(/\s+/g, " ").trim();

  const affordanceFor = (element, text) => {
    const wanted = text.slice(0, 200);
    for (let node = element, depth = 0; node && depth < 6; node = node.parentElement, depth += 1) {
      for (const attribute of ["title", "data-tip"]) {
        const value = node.getAttribute?.(attribute);
        if (value && value.replace(/\s+/g, " ").includes(wanted)) return attribute;
      }
      if (node !== element && node.matches?.(CONTROL)) break;
    }
    return null;
  };

  const isInlineLevel = (node) => node.nodeType === Node.TEXT_NODE
    || (node.nodeType === Node.ELEMENT_NODE && /^inline($| )|^contents$/.test(getComputedStyle(node).display));

  // Text containers: a box whose children are text or inline content.
  const opaqueBackground = (element) => {
    const style = getComputedStyle(element);
    if (/^(IMG|VIDEO|CANVAS|svg)$/i.test(element.tagName)) return true;
    const match = /rgba?\(([^)]+)\)/.exec(style.backgroundColor);
    const alpha = match ? Number(match[1].split(",")[3] ?? 1) : 0;
    return alpha >= 0.9 || style.backgroundImage !== "none";
  };
  const nearlyInvisible = (element) => {
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      if (Number(getComputedStyle(node).opacity) < 0.1) return true;
    }
    return false;
  };

  // Is text covered by an opaque layer? Hit-tested with pointer events on for
  // everything, so a decorative layer that ignores the pointer still counts.
  const textCoverage = (element, box) => {
    const points = [0.5, 0.2, 0.8].map((fraction) => [box.x + box.w * fraction, box.y + box.h / 2]);
    const hits = [];
    for (const [x, y] of points) {
      const hit = document.elementFromPoint(x, y);
      if (!hit || hit === element || element.contains(hit) || hit.contains(element) || nearlyInvisible(hit)) continue;
      if (hit.closest(".tooltip-bubble,[role=tooltip],[role=listbox],[role=menu]")) continue;
      let covering = null;
      for (let node = hit; node && !node.contains(element); node = node.parentElement) {
        if (opaqueBackground(node)) {
          covering = node;
          break;
        }
      }
      if (!covering) continue;
      const overlay = overlayOf(covering);
      const coveringOverlay = overlay && !overlay.contains(element) ? overlay : null;
      const popup = covering.closest("[role=dialog],[role=alertdialog]");
      hits.push({
        point: "center",
        hitDesc: describe(covering),
        overlayDesc: coveringOverlay ? describe(coveringOverlay) : null,
        overlayRect: coveringOverlay ? round(rectOf(coveringOverlay)) : null,
        inPopup: Boolean(popup && !popup.contains(element)),
      });
      break;
    }
    if (!hits.length) return null;
    const scroller = scrollerOf(element);
    return {
      hits,
      remainingUp: scroller ? scroller.scrollTop : 0,
      remainingDown: scroller ? Math.max(0, scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop) : 0,
    };
  };

  const textBlocks = () => {
    const blocks = [];
    const pointerAll = document.createElement("style");
    pointerAll.textContent = "*,*::before,*::after{pointer-events:auto!important}";
    document.head.append(pointerAll);
    try {
      for (const element of document.body.querySelectorAll("*")) {
        const block = measureText(element);
        if (block) blocks.push(block);
      }
    } finally {
      pointerAll.remove();
    }
    return blocks;
  };

  const measureText = (element) => {
    if (TEXT_INPUT.test(element.tagName) || element.closest("svg,script,style,noscript")) return null;
    const style = getComputedStyle(element);
    if (/^inline($| )|^contents$|^none$/.test(style.display)) return null;
    const text = fullText(element);
    if (!text) return null;
    const children = [...element.childNodes];
    const hasDirectText = children.some((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    if (!hasDirectText && !(element.children.length && children.every(isInlineLevel))) return null;
    if (!rendered(element) || isInert(element) || element.closest(".tooltip-bubble")) return null;
    const rect = rectOf(element);
    if (visuallyHidden(element, rect)) return null;
    const overflowX = element.scrollWidth - element.clientWidth;
    const overflowY = element.scrollHeight - element.clientHeight;
    // The drawn text, leaving out visually hidden helper text (sr-only spans).
    const textRect = textRectOf(element);
    const textBox = textRect
      ? { left: textRect.x, top: textRect.y, right: textRect.x + textRect.w, bottom: textRect.y + textRect.h, width: textRect.w, height: textRect.h }
      : { left: rect.x, top: rect.y, right: rect.x + rect.w, bottom: rect.y + rect.h, width: 0, height: 0 };
    let spill = null;
    if ((overflowX > 1 && style.overflowX === "visible") || (overflowY > 1 && style.overflowY === "visible")) {
      const right = textBox.right - (rect.x + rect.w);
      const bottom = textBox.bottom - (rect.y + rect.h);
      if (right > 1 || bottom > 1) spill = { right: Math.round(right), bottom: Math.round(bottom) };
    }
    const chain = clipChain(element);
    if (!visibleRect(rect, chain)) return null;
    // The text as drawn: an element that clips itself shows at most its own box
    // (its own truncation is judged separately); otherwise the text's extent.
    const ownClip = style.overflowX !== "visible" || style.overflowY !== "visible";
    const drawn = ownClip ? intersect({ x: textBox.left, y: textBox.top, w: textBox.width, h: textBox.height }, rect) : { x: textBox.left, y: textBox.top, w: textBox.width, h: textBox.height };
    const shownPart = drawn && visibleRect(drawn, chain);
    // Decorative glyphs hidden from assistive tech (the ASCII progress bar's
    // cells, cropped to the bar's height by design) are not text to be read.
    const readable = !element.closest("[aria-hidden='true']");
    const ancestorClipped = readable && Boolean(drawn && drawn.w > 2 && drawn.h > 2
      && (!shownPart || shownPart.w < drawn.w - 1 || shownPart.h < drawn.h - 1));
    const covered = readable && shownPart && shownPart.w > 2 && shownPart.h > 2 ? textCoverage(element, shownPart) : null;
    if (overflowX <= 1 && overflowY <= 1 && !spill && !ancestorClipped && !covered) return null;
    return {
      id: idOf(element),
      desc: describe(element),
      text: text.slice(0, 120),
      rect: round(rect),
      overflowX,
      overflowY,
      styleOverflowX: style.overflowX,
      styleOverflowY: style.overflowY,
      textOverflow: style.textOverflow,
      lineClamp: style.webkitLineClamp && style.webkitLineClamp !== "none" ? style.webkitLineClamp : null,
      whiteSpace: style.whiteSpace,
      lineHeight: parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2,
      affordance: affordanceFor(element, text),
      spill,
      textRect: ancestorClipped ? round(drawn) : null,
      chain: ancestorClipped ? chain : null,
      coverage: covered,
    };
  };

  const interactiveElements = () => {
    const seen = new Set();
    const items = [];
    for (const element of document.querySelectorAll(INTERACTIVE)) {
      if (seen.has(element) || !rendered(element)) continue;
      seen.add(element);
      const rect = rectOf(element);
      if (rect.w === 0 && rect.h === 0) continue;
      items.push(element);
    }
    return items;
  };

  const elementFacts = (element) => {
    const rect = rectOf(element);
    const chain = clipChain(element);
    const hidden = visuallyHidden(element, rect);
    return {
      id: idOf(element),
      desc: describe(element),
      tag: element.tagName.toLowerCase(),
      type: element.getAttribute("type"),
      role: element.getAttribute("role"),
      rect: round(rect),
      hidden,
      inert: isInert(element),
      disabled: element.matches(":disabled") || element.getAttribute("aria-disabled") === "true",
      tip: element.getAttribute("data-tip"),
      title: element.getAttribute("title"),
      hasPopup: element.getAttribute("aria-haspopup"),
      expanded: element.getAttribute("aria-expanded"),
      controls: element.getAttribute("aria-controls"),
      primary: element.matches(".btn-primary,button[type=submit]"),
      focusable: element.tabIndex >= 0 && !element.matches(":disabled"),
      chain,
      coverage: hidden ? null : coverage(element, rect, chain),
    };
  };

  const insidePopup = (element) => {
    for (let ancestor = element.parentElement; ancestor && ancestor !== document.body; ancestor = ancestor.parentElement) {
      if (isPopup(ancestor, getComputedStyle(ancestor))) return true;
    }
    return false;
  };

  // Top-level floating layers. A listbox inside a menu is that menu's content
  // (it scrolls inside it), so only the outermost popup is measured.
  const popups = () => {
    const found = [];
    for (const element of document.body.querySelectorAll("*")) {
      const style = getComputedStyle(element);
      const popup = isPopup(element, style);
      if (style.position !== "fixed" && !popup) continue;
      if (!rendered(element)) continue;
      const rect = rectOf(element);
      if (rect.w === 0 || rect.h === 0 || visuallyHidden(element, rect)) continue;
      if (popup && insidePopup(element)) continue;
      // A fixed control parked wholly off screen (a skip link before it is
      // focused) is hidden by design; the focus checks bring it on screen.
      if (!popup && !intersect(rect, viewportBox())) continue;
      found.push({
        id: idOf(element),
        domId: element.id || null,
        desc: describe(element),
        role: element.getAttribute("role"),
        popup,
        fixed: style.position === "fixed",
        tooltip: element.classList.contains("tooltip-bubble") || element.getAttribute("role") === "tooltip",
        // A tooltip exists to show text in full; its own text must not be cut.
        textCut: (element.classList.contains("tooltip-bubble") || element.getAttribute("role") === "tooltip")
          && [element, ...element.querySelectorAll("*")].some((node) => {
            const nodeStyle = getComputedStyle(node);
            const clips = nodeStyle.overflowX !== "visible" || nodeStyle.overflowY !== "visible";
            return (clips && (node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1))
              || (nodeStyle.textOverflow === "ellipsis" && node.scrollWidth > node.clientWidth + 1);
          }),
        rect: round(rect),
        chain: style.position === "fixed" ? [] : clipChain(element),
      });
    }
    return found;
  };

  // Outline colour, width and offset are invisible while the outline style is
  // none; they must not count as a visible focus change.
  const visibleFocusProps = (node) => {
    const style = getComputedStyle(node);
    const values = FOCUS_PROPS.map((property) => style.getPropertyValue(property));
    if (values[0] === "none" || parseFloat(values[1]) === 0) values.splice(0, 4, "none", "none", "none", "none");
    return values;
  };
  // The focused box, its parent and grandparent (for :focus-within designs),
  // and its first few descendants (a card that rings its cover image).
  const FOCUS_DESCENDANTS = 6;
  const focusStyles = (element) => {
    const styles = [];
    for (let node = element, depth = 0; node && depth < 3 && node !== document.body; node = node.parentElement, depth += 1) {
      styles.push(visibleFocusProps(node));
    }
    while (styles.length < 3) styles.push(null);
    for (const node of [...element.querySelectorAll("*")].slice(0, FOCUS_DESCENDANTS)) styles.push(visibleFocusProps(node));
    return styles;
  };
  // An element that appeared after the baseline (a lazily added row) is
  // compared with an unfocused element of the same kind.
  const siblingBaseline = (element) => {
    const className = typeof element.className === "string" ? element.className.trim().split(/\s+/)[0] : "";
    if (!className) return null;
    const twin = [...document.querySelectorAll(`${element.tagName.toLowerCase()}.${CSS.escape(className)}`)]
      .find((candidate) => candidate !== element && !candidate.contains(document.activeElement) && rendered(candidate));
    return twin ? focusStyles(visibleTarget(twin)) : null;
  };

  // The box a keyboard user sees: a visually hidden control (a native radio
  // inside a styled label) shows its focus on the nearest visible ancestor.
  const visibleTarget = (element) => {
    // Also a transparent native input laid over its label (opacity 0): the
    // label is what shows the focus.
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      if (!visuallyHidden(node) && rendered(node)) return node;
    }
    return element;
  };

  const ringRect = (element) => {
    const rect = rectOf(element);
    const style = getComputedStyle(element);
    let grow = 0;
    if (style.outlineStyle !== "none") grow = Math.max(grow, parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset));
    if (style.boxShadow !== "none") {
      for (const shadow of style.boxShadow.split(/,(?![^(]*\))/)) {
        if (/inset/.test(shadow)) continue;
        const numbers = (shadow.replace(/rgba?\([^)]*\)/g, "").match(/-?[\d.]+px/g) ?? []).map(parseFloat);
        const [x = 0, y = 0, blur = 0, spread = 0] = numbers;
        grow = Math.max(grow, spread + Math.max(Math.abs(x), Math.abs(y)) + blur / 2);
      }
    }
    grow = Math.max(0, grow);
    return { x: rect.x - grow, y: rect.y - grow, w: rect.w + 2 * grow, h: rect.h + 2 * grow, grow };
  };

  const layoutTracked = () => {
    const tracked = new Map();
    const viewport = viewportBox();
    for (const element of interactiveElements()) {
      if (isInert(element) || element.closest(".tooltip-bubble")) continue;
      const rect = rectOf(element);
      if (!intersect(rect, viewport)) continue;
      tracked.set(idOf(element), round(rect));
    }
    return tracked;
  };

  // The union box of an element's text nodes, or null without text.
  const textRectOf = (element) => {
    let box = null;
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim()) continue;
      const holder = node.parentElement;
      if (holder && holder !== element && (visuallyHidden(holder) || !rendered(holder))) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      box = box
        ? { x: Math.min(box.x, r.left), y: Math.min(box.y, r.top), w: Math.max(box.x + box.w, r.right) - Math.min(box.x, r.left), h: Math.max(box.y + box.h, r.bottom) - Math.min(box.y, r.top) }
        : { x: r.left, y: r.top, w: r.width, h: r.height };
    }
    return box;
  };

  // The app's focus rescue announces every time it caught focus falling to
  // <body>. "refocus" means the control was still there: something dropped
  // focus by mistake, which the audit reports even though it was repaired.
  const rescues = [];
  document.addEventListener("focusrescue", (event) => {
    rescues.push({ kind: event.detail?.kind, desc: describe(event.detail?.target) });
  });

  const focusBaseline = new Map();
  let disabledTransitions = null;

  window.__interactionProbe = {
    idOf,
    describeId: (id) => describe(byId(id)),
    exists: (id) => Boolean(byId(id)?.isConnected),
    // Read end-state styles: transitions would otherwise be caught mid-way.
    disableTransitions() {
      if (disabledTransitions?.isConnected) return;
      disabledTransitions = document.createElement("style");
      disabledTransitions.textContent = "*,*::before,*::after{transition-duration:0s!important;transition-delay:0s!important}";
      document.head.append(disabledTransitions);
    },
    page() {
      const root = document.scrollingElement ?? document.documentElement;
      return {
        viewport: { w: innerWidth, h: innerHeight, cw: document.documentElement.clientWidth, ch: document.documentElement.clientHeight },
        horizontalScroll: Math.max(0, root.scrollWidth - root.clientWidth),
        scrollX: window.scrollX,
      };
    },
    snapshot() {
      return {
        page: this.page(),
        elements: interactiveElements().map(elementFacts),
        popups: popups(),
        texts: textBlocks(),
        scrollAreas: scrollAreasUnderBars(),
      };
    },
    elements() {
      return interactiveElements().filter((element) => !isInert(element)).map((element) => {
        const rect = rectOf(element);
        return {
          id: idOf(element),
          desc: describe(element),
          rect: round(rect),
          hidden: visuallyHidden(element, rect),
          disabled: element.matches(":disabled") || element.getAttribute("aria-disabled") === "true",
          tip: element.getAttribute("data-tip"),
          title: element.getAttribute("title"),
          hasPopup: element.getAttribute("aria-haspopup"),
          expanded: element.getAttribute("aria-expanded"),
          tag: element.tagName.toLowerCase(),
          role: element.getAttribute("role"),
          signature: `${element.tagName}|${typeof element.className === "string" ? element.className : ""}|${element.getAttribute("role") ?? ""}`,
        };
      });
    },
    facts: (ids) => ids.map((id) => byId(id)).filter(Boolean).map(elementFacts),
    popups,
    textBlocks,
    layout: () => Object.fromEntries(layoutTracked()),
    // Bring an element on screen the way a user would (nearest edge), return its centre.
    reveal(id) {
      const element = byId(id);
      if (!element || !element.isConnected) return null;
      element.scrollIntoView({ block: "nearest", inline: "nearest" });
      const rect = rectOf(element);
      const chain = clipChain(element);
      const visible = visibleRect(rect, chain);
      if (!visible) return null;
      return { x: visible.x + visible.w / 2, y: visible.y + visible.h / 2, rect: round(rect) };
    },
    captureFocusBaseline() {
      focusBaseline.clear();
      for (const element of interactiveElements()) {
        if (element === document.activeElement) continue;
        focusBaseline.set(idOf(element), focusStyles(visibleTarget(element)));
      }
    },
    focusInfo() {
      const active = document.activeElement;
      if (!active || active === document.body || active === document.documentElement) {
        return { none: true, desc: describe(active) };
      }
      const target = visibleTarget(active);
      const rect = rectOf(target);
      const chain = clipChain(target);
      const ring = ringRect(target);
      const baseline = focusBaseline.get(idOf(active)) ?? siblingBaseline(target);
      return {
        none: false,
        id: idOf(active),
        desc: describe(active),
        targetDesc: describe(target),
        rect: round(rect),
        ring: round(ring),
        ringGrow: ring.grow,
        hidden: visuallyHidden(target, rect),
        inViewport: Boolean(intersect(rect, viewportBox())),
        chain,
        coverage: coverage(target, rect, chain),
        baseline,
        now: focusStyles(target),
        expanded: active.getAttribute("aria-expanded"),
        hasPopup: active.getAttribute("aria-haspopup"),
        controls: active.getAttribute("aria-controls"),
        tag: active.tagName.toLowerCase(),
        role: active.getAttribute("role"),
        tip: active.closest("[data-tip]")?.getAttribute("data-tip") ?? null,
        tipAnchor: active.closest("[data-tip]") ? round(rectOf(active.closest("[data-tip]"))) : null,
        inDialog: active.closest("[role=dialog],[role=alertdialog]") ? idOf(active.closest("[role=dialog],[role=alertdialog]")) : null,
        signature: `${active.tagName}|${typeof active.className === "string" ? active.className : ""}|${active.getAttribute("role") ?? ""}`,
      };
    },
    activeId: () => {
      const active = document.activeElement;
      return active && active !== document.body ? idOf(active) : null;
    },
    // Disclosure state of an element (or the <details> owning a <summary>).
    disclosureState(id) {
      const element = byId(id);
      if (!element || !element.isConnected) return { connected: false };
      const details = element.tagName === "SUMMARY" ? element.parentElement : null;
      return {
        connected: true,
        visible: rendered(element) && !visuallyHidden(element),
        expanded: element.getAttribute("aria-expanded"),
        detailsOpen: details?.tagName === "DETAILS" ? details.open : null,
        activeInside: false,
      };
    },
    dialogs() {
      return [...document.querySelectorAll("[role=dialog],[role=alertdialog],dialog[open]")]
        .filter((element) => rendered(element))
        .map((element) => ({ id: idOf(element), desc: describe(element), modal: element.getAttribute("aria-modal") === "true" }));
    },
    contains: (containerId, id) => {
      const container = byId(containerId);
      const element = byId(id);
      return Boolean(container && element && container.contains(element));
    },
    focusedWithin: (containerId) => {
      const container = byId(containerId);
      return Boolean(container && container.contains(document.activeElement));
    },
    controlsTarget(id) {
      const element = byId(id);
      return element?.getAttribute("aria-controls") ?? null;
    },
    scrollContainers() {
      const found = [];
      for (const element of document.body.querySelectorAll("*")) {
        const style = getComputedStyle(element);
        if (!/(auto|scroll|overlay)/.test(style.overflowY) || element.scrollHeight <= element.clientHeight + 1) continue;
        if (!rendered(element) || isInert(element)) continue;
        found.push({ id: idOf(element), desc: describe(element), scrollTop: element.scrollTop });
      }
      return found;
    },
    scrollTo(id, top) {
      const element = byId(id);
      if (!element) return null;
      element.scrollTop = top === "end" ? element.scrollHeight : top;
      return element.scrollTop;
    },
    factsWithin(containerId) {
      const container = byId(containerId);
      if (!container) return [];
      return interactiveElements().filter((element) => container.contains(element) && !isInert(element)).map(elementFacts);
    },
    // Ellipsized text inside a focused control that has no title of its own:
    // the app must reveal it in a tooltip.
    cutShortTexts(id) {
      const control = byId(id);
      if (!control) return [];
      const inside = control.querySelectorAll("*");
      const texts = [];
      for (const element of inside.length > 40 ? [control] : [control, ...inside]) {
        if (!rendered(element)) continue;
        const titled = element.closest("[title]");
        if (titled && (titled === control || control.contains(titled))) continue;
        const style = getComputedStyle(element);
        const cut = (style.textOverflow === "ellipsis" && element.scrollWidth > element.clientWidth + 1)
          || (style.webkitLineClamp && style.webkitLineClamp !== "none" && element.scrollHeight > element.clientHeight + 1);
        if (cut) texts.push(fullText(element));
      }
      return texts.filter(Boolean);
    },
    tooltipText() {
      const bubble = document.querySelector(".tooltip-bubble.is-placed");
      return bubble ? fullText(bubble) : null;
    },
    // The box that carries the focus indicator: the node among the focused box,
    // its ancestors and first descendants whose focus styles changed most
    // visibly (an outline or shadow first), with that node's ring geometry.
    indicatorBox() {
      const active = document.activeElement;
      if (!active || active === document.body) return null;
      const target = visibleTarget(active);
      const nodes = [];
      for (let node = target, depth = 0; node && depth < 3 && node !== document.body; node = node.parentElement, depth += 1) nodes.push(node);
      while (nodes.length < 3) nodes.push(null);
      nodes.push(...[...target.querySelectorAll("*")].slice(0, FOCUS_DESCENDANTS));
      const baseline = focusBaseline.get(idOf(active)) ?? siblingBaseline(target);
      const now = focusStyles(target);
      const changed = nodes.map((node, index) => node && baseline?.[index] && now[index]
        ? now[index].some((value, at) => value !== baseline[index][at]) : false);
      const ringOf = (node) => {
        const style = getComputedStyle(node);
        const radius = parseFloat(style.borderTopLeftRadius) || 0;
        if (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0) {
          return { width: parseFloat(style.outlineWidth), offset: parseFloat(style.outlineOffset) || 0, radius };
        }
        if (style.boxShadow !== "none") {
          const numbers = (style.boxShadow.replace(/rgba?\([^)]*\)/g, "").match(/-?[\d.]+px/g) ?? []).map(parseFloat);
          const spread = numbers[3] ?? 0;
          if (spread > 0) return { width: spread, offset: /inset/.test(style.boxShadow) ? -spread : 0, radius };
        }
        return null;
      };
      let index = nodes.findIndex((node, at) => changed[at] && node && ringOf(node));
      if (index < 0) index = changed.findIndex(Boolean);
      const node = nodes[index] ?? target;
      const box = rectOf(node);
      const style = getComputedStyle(node);
      return {
        desc: describe(active),
        box: round(box),
        ring: ringOf(node),
        radius: parseFloat(style.borderTopLeftRadius) || 0,
        key: [node.tagName, typeof node.className === "string" ? node.className : "", style.outlineStyle, style.outlineColor,
          style.outlineWidth, style.outlineOffset, style.boxShadow, style.backgroundColor, style.borderTopColor,
          Math.round(box.width / 8), Math.round(box.height / 8)].join("|"),
      };
    },
    // Move focus somewhere real but out of sight, so nothing falls to <body>.
    parkFocus() {
      let sentinel = document.querySelector("[data-interaction-sentinel]");
      if (!sentinel) {
        sentinel = document.createElement("span");
        sentinel.tabIndex = -1;
        sentinel.setAttribute("data-interaction-sentinel", "");
        Object.assign(sentinel.style, { position: "fixed", left: "-40px", top: "-40px", width: "1px", height: "1px", outline: "none" });
        document.body.append(sentinel);
      }
      sentinel.focus();
    },
    restoreFocus(id) {
      byId(id)?.focus({ preventScroll: true });
      document.querySelector("[data-interaction-sentinel]")?.remove();
    },
    // Where focus is after an activation: on something real, visible and on
    // screen, inside the modal if one is open.
    focusSanity() {
      const active = document.activeElement;
      const modal = [...document.querySelectorAll("[aria-modal='true']")].find((element) => rendered(element));
      if (!active || active === document.body || active === document.documentElement) {
        return { ok: false, reason: "focus fell back to <body>", desc: describe(active) };
      }
      const target = visibleTarget(active);
      const rect = rectOf(target);
      if (!rendered(target) || visuallyHidden(target, rect)) return { ok: false, reason: "focus is on an element with no visible box", desc: describe(active) };
      if (!intersect(rect, viewportBox())) return { ok: false, reason: "focus is off screen", desc: describe(active) };
      if (modal && !modal.contains(active)) return { ok: false, reason: `focus is outside the open modal ${describe(modal)}`, desc: describe(active) };
      return { ok: true, desc: describe(active) };
    },
    // Ordinary buttons the activation pass presses (disclosures, popups and
    // tabs have their own tests).
    activationTargets() {
      return interactiveElements().filter((element) => {
        if (!element.matches("button, [role=button]") || isInert(element)) return false;
        if (element.matches(":disabled, [aria-disabled='true'], [aria-haspopup]:not([aria-haspopup='false']), [aria-expanded], [role=tab], summary")) return false;
        const rect = rectOf(element);
        return !visuallyHidden(element, rect);
      }).map((element) => ({
        id: idOf(element),
        desc: describe(element),
        key: `${element.tagName}|${typeof element.className === "string" ? element.className.replace(/\bis-[\w-]+/g, "").trim() : ""}|${accessibleName(element).replace(/\d+/g, "#").slice(0, 40)}`,
      }));
    },
    // Which view is showing: a change means the activation left the state.
    viewFingerprint() {
      const panels = [...document.querySelectorAll("[role=tabpanel]")].filter((panel) => !panel.hidden).map((panel) => `${panel.id}:${panel.className}`);
      const dialogs = [...document.querySelectorAll("[role=dialog],[role=alertdialog]")].filter((element) => rendered(element)).length;
      const setup = Boolean(document.querySelector(".setup-frame"));
      return JSON.stringify({ panels, dialogs, setup, confirm: document.querySelectorAll(".inline-confirm").length });
    },
    usable(id) {
      const element = byId(id);
      return Boolean(element && element.isConnected && rendered(element) && !element.matches(":disabled") && !isInert(element));
    },
    focusId(id) {
      const element = byId(id);
      element?.scrollIntoView({ block: "nearest", inline: "nearest" });
      element?.focus();
      return document.activeElement === element;
    },
    // Forced colours: controls with a text label, and selected-state pairs.
    labelledControls() {
      return interactiveElements().filter((element) => element.matches("button, [role=button], [role=tab], a[href], summary, label")
        && !isInert(element) && !visuallyHidden(element) && fullText(element).length > 1)
        .map((element) => ({ id: idOf(element), desc: describe(element), key: `${element.tagName}|${typeof element.className === "string" ? element.className : ""}|${element.matches(":disabled")}` }));
    },
    selectedPairs() {
      const pairs = [];
      const selectedSelector = "[aria-selected='true'],[aria-current='true'],[aria-current='page'],[aria-pressed='true'],.is-active,.active,.is-current";
      for (const selected of document.querySelectorAll(selectedSelector)) {
        if (!rendered(selected) || isInert(selected) || !selected.matches("button, [role=tab], [role=option], label, a")) continue;
        const kind = selected.tagName;
        const siblings = [...(selected.parentElement?.children ?? [])];
        const plain = siblings.find((other) => other !== selected && other.tagName === kind && rendered(other) && !other.matches(selectedSelector));
        if (plain) pairs.push({ selected: idOf(selected), plain: idOf(plain), desc: describe(selected), key: `${kind}|${typeof selected.className === "string" ? selected.className : ""}` });
      }
      return pairs;
    },
    // The box of a control's text (where a backplate would be drawn).
    labelBox(id) {
      const element = byId(id);
      if (!element || !rendered(element)) return null;
      element.scrollIntoView({ block: "nearest", inline: "nearest" });
      const textBox = textRectOf(element);
      return textBox ? round(intersect(textBox, rectOf(element)) ?? textBox) : null;
    },
    stateBoxes(selectedId, plainId) {
      const selected = byId(selectedId);
      const plain = byId(plainId);
      if (!selected || !plain) return null;
      const entry = (element) => ({ box: round(rectOf(element)), text: textRectOf(element) && round(textRectOf(element)) });
      return { selected: entry(selected), plain: entry(plain) };
    },
    hideText(id, hidden) {
      const element = byId(id);
      if (!element) return;
      if (hidden) element.setAttribute("data-interaction-no-text", "");
      else element.removeAttribute("data-interaction-no-text");
      let style = document.querySelector("style[data-interaction-no-text]");
      if (!style) {
        style = document.createElement("style");
        style.setAttribute("data-interaction-no-text", "");
        style.textContent = "[data-interaction-no-text],[data-interaction-no-text] *{color:transparent!important;-webkit-text-fill-color:transparent!important}";
        document.head.append(style);
      }
    },
    takeRescues: () => rescues.splice(0, rescues.length),
    // "No focus" for the hover pass and the start of a Tab walk: focus parks
    // on an off-screen sentinel, a real focus move, so the app never sees focus
    // fall back to <body> (which its focus rescue would undo).
    blurActive() {
      this.parkFocus();
    },
    // Highlight an element for a failure screenshot; returns a remover id.
    mark(id, rect) {
      const element = byId(id);
      const box = rect ?? (element ? rectOf(element) : null);
      if (!box) return false;
      const marker = document.createElement("div");
      marker.setAttribute("data-interaction-marker", "");
      Object.assign(marker.style, {
        position: "fixed", left: `${box.x - 2}px`, top: `${box.y - 2}px`, width: `${box.w + 4}px`, height: `${box.h + 4}px`,
        outline: "2px dashed #ff2d55", pointerEvents: "none", zIndex: "2147483647",
      });
      document.body.append(marker);
      return true;
    },
    unmark() {
      for (const marker of document.querySelectorAll("[data-interaction-marker]")) marker.remove();
    },
  };
}
