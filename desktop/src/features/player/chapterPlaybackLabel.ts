export function chapterPlaybackStatusLabel(input: {
  isCurrent: boolean;
  playing: boolean;
}): "Playing" | "Selected" | null {
  if (!input.isCurrent) {
    return null;
  }
  return input.playing ? "Playing" : "Selected";
}

// "Chapter 3", "CHAPTER 3:", "Chapter 3 -" and "3." already name the chapter.
function titleNamesChapter(title: string, number: number): boolean {
  const pattern = new RegExp(`^(chapter\\s+${number}(?!\\d)|${number}\\s*[.):])`, "i");
  return pattern.test(title.trim());
}

/** "Chapter N: Title", without repeating a number the title already carries. */
export function chapterDisplayLabel(chapter: { number: number; title?: string | null } | null | undefined): string {
  if (!chapter) return "Chapter unknown";
  const title = chapter.title?.trim() ?? "";
  if (!title) return `Chapter ${chapter.number}`;
  return titleNamesChapter(title, chapter.number) ? title : `Chapter ${chapter.number}: ${title}`;
}
