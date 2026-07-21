import { useCallback } from "react";

import type { TermSelectOption } from "../../types/api";
import { useComboboxState } from "./useComboboxState";
import "./term-select.css";

interface TermSelectProps {
  id: string;
  className?: string;
  label?: string;
  value: string;
  options: TermSelectOption[];
  placeholder: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}

export function TermSelect({ id, className, label, value, options, placeholder, disabled, onChange }: TermSelectProps) {
  const menuId = `${id}-listbox`;
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selectedOption = selectedIndex >= 0 ? options[selectedIndex] : null;

  const onChoose = useCallback(
    (index: number) => {
      const option = options[index];
      if (option) {
        onChange(option.value);
      }
    },
    [onChange, options],
  );

  const { open, highlightIndex, rootRef, toggle, highlightTo, choose, handleKeyDown } = useComboboxState({
    optionCount: options.length,
    selectedIndex,
    disabled: Boolean(disabled),
    onChoose,
  });

  const activeDescendantId = open && highlightIndex >= 0 ? `${menuId}-option-${highlightIndex}` : undefined;

  return (
    <div id={id} className={`term-select${className ? ` ${className}` : ""}`} ref={rootRef}>
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
        onClick={toggle}
        onKeyDown={handleKeyDown}
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
                onClick={() => choose(index)}
                onMouseEnter={() => highlightTo(index)}
                style={isActive ? { background: "var(--surface-hover)" } : undefined}
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
