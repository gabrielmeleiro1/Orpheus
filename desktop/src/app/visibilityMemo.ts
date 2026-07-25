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

export function equalWhenHidden<T extends { hidden: boolean }>(
  previous: Readonly<T>,
  next: Readonly<T>,
): boolean {
  return (previous.hidden && next.hidden) || shallowEqual(previous, next);
}

export function equalWhenInvisible<T extends { visible: boolean }>(
  previous: Readonly<T>,
  next: Readonly<T>,
): boolean {
  return (!previous.visible && !next.visible) || shallowEqual(previous, next);
}
