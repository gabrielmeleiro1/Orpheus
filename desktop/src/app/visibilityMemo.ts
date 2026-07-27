import { createElement, memo, type ComponentType } from "react";

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

/**
 * Wrap a panel that has no `hidden` prop of its own in the hidden-aware memo:
 * the caller passes `hidden` purely as a render gate and it is stripped before
 * the component sees it.
 */
export function memoWhenHidden<P extends object>(Component: ComponentType<P>) {
  const Wrapped = ({ hidden: _hidden, ...props }: P & { hidden: boolean }) =>
    createElement(Component, props as unknown as P);
  Wrapped.displayName = `MemoWhenHidden(${Component.displayName || Component.name || "Component"})`;
  return memo(Wrapped, equalWhenHidden);
}
