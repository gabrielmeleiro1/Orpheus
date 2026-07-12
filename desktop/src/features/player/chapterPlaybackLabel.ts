export function chapterPlaybackStatusLabel(input: {
  isCurrent: boolean;
  playing: boolean;
}): "Playing" | "Selected" | null {
  if (!input.isCurrent) {
    return null;
  }
  return input.playing ? "Playing" : "Selected";
}
