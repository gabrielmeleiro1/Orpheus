import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type { TermSelectOption } from "../../types/api";
import "./term-select.css";

interface TermSelectProps {
  id: string;
  label?: string;
  value: string;
  options: TermSelectOption[];
  placeholder: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}

export function TermSelect({ id, label, value, options, placeholder, disabled, onChange }: TermSelectProps) {
  const [open, setOpen] = useState(false);
  const [highlightIndex, setHighlightIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const menuId = `${id}-listbox`;
  const activeDescendantId = open && highlightIndex >= 0 ? `${menuId}-option-${highlightIndex}` : undefined;
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selectedOption = selectedIndex >= 0 ? options[selectedIndex] : null;

  useEffect(() => {
    if (!open) {
      return;
    }

    setHighlightIndex(selectedIndex >= 0 ? selectedIndex : 0);
  }, [open, selectedIndex]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current && !rootRef.current.contains(target)) {
        setOpen(false);
      }
    };

    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  const choose = useCallback(
    (option: TermSelectOption) => {
      onChange(option.value);
      setOpen(false);
    },
    [onChange],
  );

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      if (disabled || options.length === 0) {
        return;
      }

      if (event.key === "Escape") {
        if (open) {
          event.preventDefault();
          setOpen(false);
        }
        return;
      }

      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!open) {
          setOpen(true);
          return;
        }
        setHighlightIndex((index) => Math.min(options.length - 1, Math.max(0, index + 1)));
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        if (!open) {
          setOpen(true);
          return;
        }
        setHighlightIndex((index) => Math.max(0, index <= 0 ? 0 : index - 1));
        return;
      }

      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (!open) {
          setOpen(true);
          return;
        }
        const index = highlightIndex >= 0 ? highlightIndex : selectedIndex >= 0 ? selectedIndex : 0;
        if (options[index]) {
          choose(options[index]);
        }
      }
    },
    [choose, disabled, highlightIndex, open, options, selectedIndex],
  );

  return (
    <div id={id} className="term-select" ref={rootRef}>
      <button
        type="button"
        className="term-select-btn"
        role="combobox"
        aria-label={label ?? placeholder}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={menuId}
        aria-activedescendant={activeDescendantId}
        disabled={disabled || options.length === 0}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={onKeyDown}
      >
        <span>{selectedOption?.label ?? placeholder}</span>
        <span className="term-select-arrow" aria-hidden="true">
          <svg viewBox="0 0 20 20" focusable="false">
            <path d={open ? "M5 12l5-5 5 5" : "M5 8l5 5 5-5"} />
          </svg>
        </span>
      </button>
      {open ? (
        <div id={menuId} className="term-select-menu" role="listbox">
          {options.map((option, index) => {
            const isSelected = option.value === selectedOption?.value;
            const isActive = index === highlightIndex;

            return (
              <div
                id={`${menuId}-option-${index}`}
                key={option.value}
                className={`term-select-option${isSelected ? " is-active" : ""}`}
                role="option"
                aria-selected={isSelected}
                onClick={() => choose(option)}
                onMouseEnter={() => setHighlightIndex(index)}
                style={isActive ? { background: "var(--hover)" } : undefined}
              >
                <span className="term-select-option-mark" aria-hidden="true" />
                <span className="term-select-option-label">{option.label}</span>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
