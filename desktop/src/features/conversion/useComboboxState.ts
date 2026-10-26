import { type KeyboardEvent, useCallback, useEffect, useReducer, useRef } from "react";

export interface ComboboxState {
  open: boolean;
  highlightIndex: number;
}

type ComboboxAction =
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
  /** A searchable menu stays open while its query matches nothing. */
  keepOpenWhenEmpty?: boolean;
  onChoose: (index: number) => void;
}

interface UseComboboxStateResult {
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
  keepOpenWhenEmpty = false,
  onChoose,
}: UseComboboxStateArgs): UseComboboxStateResult {
  const [state, dispatch] = useReducer(comboboxReducer, initialComboboxState);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const { open, highlightIndex } = state;

  useEffect(() => {
    if ((disabled || (optionCount === 0 && !keepOpenWhenEmpty)) && open) {
      dispatch({ type: "close" });
      return;
    }
    if (!open) {
      return;
    }
    const nextHighlight = optionCount > 0
      ? Math.min(optionCount - 1, Math.max(0, selectedIndex >= 0 ? selectedIndex : 0))
      : -1;
    dispatch({ type: "setHighlight", index: nextHighlight });
  }, [disabled, keepOpenWhenEmpty, open, optionCount, selectedIndex]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const closeOutside = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current && !rootRef.current.contains(target)) {
        dispatch({ type: "close" });
      }
    };

    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("focusin", closeOutside);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("focusin", closeOutside);
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

      if (event.key === "Tab") {
        dispatch({ type: "close" });
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
