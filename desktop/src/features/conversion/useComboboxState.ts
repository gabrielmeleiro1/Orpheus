import { type KeyboardEvent, useCallback, useEffect, useReducer, useRef } from "react";

export interface ComboboxState {
  open: boolean;
  highlightIndex: number;
}

export type ComboboxAction =
  | { type: "toggle" }
  | { type: "open" }
  | { type: "close" }
  | { type: "setHighlight"; index: number }
  | { type: "highlightNext"; optionCount: number }
  | { type: "highlightPrev" };

export const initialComboboxState: ComboboxState = { open: false, highlightIndex: -1 };

// Pure state machine for the combobox popover; exported for direct unit testing.
export function comboboxReducer(state: ComboboxState, action: ComboboxAction): ComboboxState {
  switch (action.type) {
    case "toggle":
      return { ...state, open: !state.open };
    case "open":
      return state.open ? state : { ...state, open: true };
    case "close":
      return state.open ? { ...state, open: false } : state;
    case "setHighlight":
      return { ...state, highlightIndex: action.index };
    case "highlightNext":
      return { ...state, highlightIndex: Math.min(action.optionCount - 1, Math.max(0, state.highlightIndex + 1)) };
    case "highlightPrev":
      return { ...state, highlightIndex: Math.max(0, state.highlightIndex <= 0 ? 0 : state.highlightIndex - 1) };
    default:
      return state;
  }
}

interface UseComboboxStateArgs {
  optionCount: number;
  selectedIndex: number;
  disabled: boolean;
  onChoose: (index: number) => void;
}

export interface UseComboboxStateResult {
  open: boolean;
  highlightIndex: number;
  rootRef: React.RefObject<HTMLDivElement>;
  toggle: () => void;
  close: () => void;
  highlightTo: (index: number) => void;
  choose: (index: number) => void;
  handleKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
}

export function useComboboxState({
  optionCount,
  selectedIndex,
  disabled,
  onChoose,
}: UseComboboxStateArgs): UseComboboxStateResult {
  const [state, dispatch] = useReducer(comboboxReducer, initialComboboxState);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const { open, highlightIndex } = state;

  useEffect(() => {
    if (!open) {
      return;
    }
    dispatch({ type: "setHighlight", index: selectedIndex >= 0 ? selectedIndex : 0 });
  }, [open, selectedIndex]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current && !rootRef.current.contains(target)) {
        dispatch({ type: "close" });
      }
    };

    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  const toggle = useCallback(() => dispatch({ type: "toggle" }), []);
  const close = useCallback(() => dispatch({ type: "close" }), []);
  const highlightTo = useCallback((index: number) => dispatch({ type: "setHighlight", index }), []);

  const choose = useCallback(
    (index: number) => {
      onChoose(index);
      dispatch({ type: "close" });
    },
    [onChoose],
  );

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      if (disabled || optionCount === 0) {
        return;
      }

      if (event.key === "Escape") {
        if (open) {
          event.preventDefault();
          dispatch({ type: "close" });
        }
        return;
      }

      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!open) {
          dispatch({ type: "open" });
          return;
        }
        dispatch({ type: "highlightNext", optionCount });
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        if (!open) {
          dispatch({ type: "open" });
          return;
        }
        dispatch({ type: "highlightPrev" });
        return;
      }

      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (!open) {
          dispatch({ type: "open" });
          return;
        }
        const index = highlightIndex >= 0 ? highlightIndex : selectedIndex >= 0 ? selectedIndex : 0;
        if (index >= 0 && index < optionCount) {
          choose(index);
        }
      }
    },
    [choose, disabled, highlightIndex, open, optionCount, selectedIndex],
  );

  return {
    open,
    highlightIndex,
    rootRef,
    toggle,
    close,
    highlightTo,
    choose,
    handleKeyDown,
  };
}
