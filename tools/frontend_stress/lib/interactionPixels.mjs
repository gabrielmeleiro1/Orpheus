// Pixel measurements for interaction-audit.mjs: what the user actually sees,
// for cases computed styles cannot settle (a focus ring's contrast against the
// colours around it, a label painted over by its own backplate in forced
// colours). Screenshots are decoded in the page (canvas), so no image library
// is needed. The judgement on the numbers lives in interactionChecks.mjs.

/** WCAG relative luminance contrast of two [r, g, b] pixels. */
function contrastRatio(a, b) {
  const channel = (value) => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const luminance = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

/** Decodes a base64 PNG into ImageData, inside the page. */
async function decodeInPage(base64) {
  const response = await fetch("data:image/png;base64," + base64);
  const bitmap = await createImageBitmap(await response.blob());
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const context = canvas.getContext("2d");
  context.drawImage(bitmap, 0, 0);
  return context.getImageData(0, 0, bitmap.width, bitmap.height);
}

/** Runs `analyse(images, extra, contrast)` in the page on decoded screenshots. */
function analyseInPage(page, pngs, extra, analyse) {
  return page.evaluate(async ([images, data, decodeSource, contrastSource, analyseSource]) => {
    const decode = new Function("return (" + decodeSource + ")")();
    const contrast = new Function("return (" + contrastSource + ")")();
    const run = new Function("return (" + analyseSource + ")")();
    const decoded = await Promise.all(images.map((image) => decode(image)));
    return run(decoded, data, contrast);
  }, [pngs.map((png) => png.toString("base64")), extra, decodeInPage.toString(), contrastRatio.toString(), analyse.toString()]);
}

function clipAround(box, viewport, margin = 4) {
  const x = Math.max(0, Math.floor(box.x - margin));
  const y = Math.max(0, Math.floor(box.y - margin));
  const right = Math.min(viewport.width, Math.ceil(box.x + box.w + margin));
  const bottom = Math.min(viewport.height, Math.ceil(box.y + box.h + margin));
  return right - x > 2 && bottom - y > 2 ? { x, y, width: right - x, height: bottom - y } : null;
}

const screenshot = (page, clip) => page.screenshot({ clip, animations: "disabled", caret: "hide" });

/**
 * Screenshot the focused control's indicator, move focus to an off-screen
 * sentinel (a real focus move, so nothing falls back to <body>), screenshot
 * again and put focus back. Counts pixels that change with 3:1 contrast.
 */
export async function measureFocusIndicator(page, probe, focusId) {
  const indicator = await probe(page, "indicatorBox");
  if (!indicator) return null;
  const clip = clipAround(indicator.box, page.viewportSize());
  if (!clip) return null;
  const focused = await screenshot(page, clip);
  await probe(page, "parkFocus");
  const unfocused = await screenshot(page, clip);
  await probe(page, "restoreFocus", focusId);
  const changed3 = await analyseInPage(page, [focused, unfocused], null, (images, _, contrast) => {
    const [x, y] = images;
    if (x.width !== y.width || x.height !== y.height) return -1;
    let changed = 0;
    for (let index = 0; index < x.data.length; index += 4) {
      const p = [x.data[index], x.data[index + 1], x.data[index + 2]];
      const q = [y.data[index], y.data[index + 1], y.data[index + 2]];
      if ((p[0] !== q[0] || p[1] !== q[1] || p[2] !== q[2]) && contrast(p, q) >= 3) changed += 1;
    }
    return changed;
  });
  return { ...indicator, changed3 };
}

/**
 * In forced colours: is the control's label drawn? Inside the box of its text,
 * glyphs must contrast (3:1) with the most common colour there. A label
 * painted the colour of its own backplate reads as a solid block: no pixel in
 * the text box contrasts with it.
 */
export async function measureLabelVisibility(page, probe, elementId) {
  const box = await probe(page, "labelBox", elementId);
  if (!box) return null;
  const clip = clipAround(box, page.viewportSize(), 0);
  if (!clip) return null;
  const glyphPixels = await analyseInPage(page, [await screenshot(page, clip)], null, (images, _, contrast) => {
    const [image] = images;
    const counts = new Map();
    for (let index = 0; index < image.data.length; index += 4) {
      const key = (image.data[index] << 16) | (image.data[index + 1] << 8) | image.data[index + 2];
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    const dominant = [...counts.entries()].sort((a, b) => b[1] - a[1])[0][0];
    const back = [(dominant >> 16) & 255, (dominant >> 8) & 255, dominant & 255];
    let glyphs = 0;
    for (const [key, count] of counts) {
      if (contrast([(key >> 16) & 255, (key >> 8) & 255, key & 255], back) >= 3) glyphs += count;
    }
    return glyphs;
  });
  return { box, glyphPixels };
}

/**
 * In forced colours: does a selected/current control look different from an
 * unselected one of its kind? Outside their text, the selected one must show
 * a colour (at 3:1 against the plain one's background) that the plain one
 * does not have: a marker, outline or fill.
 */
export async function measureStateDistinct(page, probe, selectedId, plainId) {
  const boxes = await probe(page, "stateBoxes", selectedId, plainId);
  if (!boxes) return null;
  const captured = [];
  const texts = [];
  for (const { box, text } of [boxes.selected, boxes.plain]) {
    const clip = clipAround(box, page.viewportSize(), 0);
    if (!clip) return null;
    captured.push(await screenshot(page, clip));
    texts.push(text && { x: text.x - clip.x, y: text.y - clip.y, w: text.w, h: text.h });
  }
  const markerPixels = await analyseInPage(page, captured, texts, (images, textBoxes, contrast) => {
    const histogram = (image, text) => {
      const counts = new Map();
      for (let y = 0; y < image.height; y += 1) {
        for (let x = 0; x < image.width; x += 1) {
          if (text && x >= text.x - 1 && x <= text.x + text.w + 1 && y >= text.y - 1 && y <= text.y + text.h + 1) continue;
          const index = (y * image.width + x) * 4;
          const key = (image.data[index] << 16) | (image.data[index + 1] << 8) | image.data[index + 2];
          counts.set(key, (counts.get(key) ?? 0) + 1);
        }
      }
      return counts;
    };
    const rgb = (key) => [(key >> 16) & 255, (key >> 8) & 255, key & 255];
    const mine = histogram(images[0], textBoxes[0]);
    const theirs = histogram(images[1], textBoxes[1]);
    const dominant = rgb([...theirs.entries()].sort((a, b) => b[1] - a[1])[0][0]);
    let pixels = 0;
    for (const [key, count] of mine) {
      if (!theirs.has(key) && contrast(rgb(key), dominant) >= 3) pixels += count;
    }
    return pixels;
  });
  return { markerPixels };
}
