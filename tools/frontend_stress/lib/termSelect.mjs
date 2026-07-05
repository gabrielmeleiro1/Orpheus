/**
 * Helpers for driving the app's custom <TermSelect> combobox component
 * (desktop/src/features/conversion/TermSelect.tsx). It renders a button
 * (#<id> button.term-select-btn) that toggles a listbox (#<id>-listbox)
 * containing .term-select-option rows with a .term-select-option-label.
 */

export function termSelectButton(page, id) {
  return page.locator(`#${id} button.term-select-btn`);
}

export async function isTermSelectDisabled(page, id) {
  return await termSelectButton(page, id).isDisabled();
}

export async function currentTermSelectLabel(page, id) {
  return (await termSelectButton(page, id).locator("span").first().textContent())?.trim() ?? "";
}

export async function openTermSelect(page, id) {
  const button = termSelectButton(page, id);
  const expanded = await button.getAttribute("aria-expanded");
  if (expanded !== "true") {
    await button.click();
  }
  await page.locator(`#${id}-listbox`).waitFor({ state: "visible", timeout: 5_000 });
}

export async function closeTermSelect(page, id) {
  const button = termSelectButton(page, id);
  const expanded = await button.getAttribute("aria-expanded");
  if (expanded === "true") {
    await page.keyboard.press("Escape");
  }
}

/** Reads all option labels without changing the current selection. */
export async function termSelectOptions(page, id) {
  await openTermSelect(page, id);
  const labels = await page.locator(`#${id}-listbox .term-select-option-label`).allTextContents();
  await closeTermSelect(page, id);
  return labels.map((label) => label.trim());
}

/** Opens the select and clicks the option at `index`. */
export async function chooseTermSelectByIndex(page, id, index) {
  await openTermSelect(page, id);
  await page.locator(`#${id}-listbox .term-select-option`).nth(index).click();
}

/** Opens the select and clicks the option whose label matches `text` exactly. */
export async function chooseTermSelectByLabel(page, id, text) {
  await openTermSelect(page, id);
  await page.locator(`#${id}-listbox .term-select-option`, { hasText: text }).first().click();
}
