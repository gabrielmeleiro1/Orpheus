function shallowEqual(previous: object, next: object): boolean {
  const previousKeys = Object.keys(previous);
  const nextKeys = Object.keys(next);
  if (previousKeys.length !== nextKeys.length) {
    return false;
  }
  return previousKeys.every((key) => Object.is(
    previous[key as keyof typeof previous],
    next[key as keyof typeof next],
  ));
}

type VisibilityComparator<K extends string> = <T extends Record<K, boolean>>(
  previous: Readonly<T>,
  next: Readonly<T>,
) => boolean;

/** Skip re-renders whose props changed only while the panel stayed off-screen;
 * a visibility transition always renders so the panel catches up. */
function comparatorForVisibilityFlag<K extends string>(
  flag: K,
  concealedValue: boolean,
): VisibilityComparator<K> {
  return (previous, next) =>
    (previous[flag] === concealedValue && next[flag] === concealedValue) ||
    shallowEqual(previous, next);
}

export const equalWhenHidden = comparatorForVisibilityFlag("hidden", true);

export const equalWhenInvisible = comparatorForVisibilityFlag("visible", false);

export const equalWhenClosed = comparatorForVisibilityFlag("open", false);
