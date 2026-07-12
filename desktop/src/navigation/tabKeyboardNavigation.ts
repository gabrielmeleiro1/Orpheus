export function tabButtonTabIndex(activeIndex: number, tabIndex: number): 0 | -1 {
  return activeIndex === tabIndex ? 0 : -1;
}

export function nextTabIndexAfterKeyboard(input: {
  key: string;
  activeIndex: number;
  tabCount: number;
}): number | null {
  const { key, activeIndex, tabCount } = input;
  if (tabCount <= 0) {
    return null;
  }
  if (key === "ArrowRight") {
    return (activeIndex + 1) % tabCount;
  }
  if (key === "ArrowLeft") {
    return (activeIndex - 1 + tabCount) % tabCount;
  }
  if (key === "Home") {
    return 0;
  }
  if (key === "End") {
    return tabCount - 1;
  }
  return null;
}
