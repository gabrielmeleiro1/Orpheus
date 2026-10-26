import { type KeyboardEvent, useCallback, useEffect, useRef } from "react";

import type { TermSelectOption } from "../../types/api";
import { Icon } from "../../ui/icons";
import { useComboboxState } from "./useComboboxState";
import { useTermSelectPlacement } from "./useTermSelectPlacement";

/** A filter field shown at the top of the open menu; the parent owns the query and filters `options`. */
interface TermSelectSearch {
  id: string;
  label: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  emptyText: string;
}

interface TermSelectProps {
  id: string;
  className?: string;
  label?: string;
  value: string;
  options: TermSelectOption[];
  placeholder: string;
  disabled?: boolean;
  search?: TermSelectSearch;
  onChange: (value: string) => void;
}

export function TermSelect({ id, className, label, value, options, placeholder, disabled, search, onChange }: TermSelectProps) {
  const menuId = `${id}-listbox`;
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selectedOption = selectedIndex >= 0 ? options[selectedIndex] : null;
  // A search can hide the selected option; the trigger keeps showing its name.
  const lastLabelRef = useRef<{ value: string; label: string } | null>(null);
  if (selectedOption) lastLabelRef.current = { value: selectedOption.value, label: selectedOption.label };
  const triggerLabel = selectedOption?.label
    ?? (lastLabelRef.current?.value === value && value ? lastLabelRef.current.label : placeholder);
  const searching = Boolean(search?.value.trim());

  const onChoose = useCallback(
    (index: number) => {
      const option = options[index];
      if (option) {
        onChange(option.value);
      }
    },
    [onChange, options],
  );

  const { open, highlightIndex, rootRef, toggle, close, highlightTo, choose, handleKeyDown } = useComboboxState({
    optionCount: options.length,
    selectedIndex,
    disabled: Boolean(disabled),
    keepOpenWhenEmpty: Boolean(search),
    onChoose,
  });

  const menuStyle = useTermSelectPlacement(open, rootRef);
  // Home and End jump to the first and last option (APG select-only combobox).
  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (open && options.length && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      highlightTo(event.key === "Home" ? 0 : options.length - 1);
      return;
    }
    handleKeyDown(event);
  };
  const activeDescendantId = open && highlightIndex >= 0 ? `${menuId}-option-${highlightIndex}` : undefined;
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      rootRef.current?.querySelector<HTMLElement>(".is-highlighted")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [open, highlightIndex, rootRef]);

  // Typing goes straight into the search; closing clears it so the next
  // opening shows every option again.
  const wasOpenRef = useRef(false);
  const searchValue = search?.value ?? "";
  const clearSearch = search?.onChange;
  useEffect(() => {
    if (open && !wasOpenRef.current) searchRef.current?.focus();
    if (!open && wasOpenRef.current && searchValue) clearSearch?.("");
    wasOpenRef.current = open;
  }, [clearSearch, open, searchValue]);

  const closeToTrigger = useCallback(() => {
    close();
    triggerRef.current?.focus();
  }, [close]);

  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (options.length) highlightTo(Math.min(options.length - 1, highlightIndex + 1));
        return;
      case "ArrowUp":
        event.preventDefault();
        if (options.length) highlightTo(Math.max(0, highlightIndex - 1));
        return;
      case "Home":
      case "End":
        if (!options.length || !event.ctrlKey) return; // plain Home/End move the caret in the search text
        event.preventDefault();
        highlightTo(event.key === "Home" ? 0 : options.length - 1);
        return;
      case "Enter":
        event.preventDefault();
        if (highlightIndex >= 0 && highlightIndex < options.length) {
          choose(highlightIndex);
          triggerRef.current?.focus();
        }
        return;
      case "Escape":
        event.preventDefault();
        closeToTrigger();
        return;
      case "Tab":
        close();
        return;
      default:
    }
  };

  const optionList = options.map((option, index) => {
    const isSelected = option.value === value;
    const isHighlighted = index === highlightIndex;

    return (
      <div
        id={`${menuId}-option-${index}`}
        key={option.value}
        className={`term-select-option${isSelected ? " is-active" : ""}${isHighlighted ? " is-highlighted" : ""}`}
        role="option"
        aria-selected={isSelected}
        // Keep focus on the trigger. A focusable ancestor scrollport
        // would otherwise close this menu before the option's click.
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          choose(index);
          if (search) triggerRef.current?.focus();
        }}
        onMouseEnter={() => highlightTo(index)}
      >
        <span className="term-select-option-mark" aria-hidden="true" />
        <span className="term-select-option-label">{option.label}</span>
      </div>
    );
  });

  return (
    <div id={id} className={`term-select${search ? " is-searchable" : ""}${className ? ` ${className}` : ""}`} ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="term-select-btn"
        role="combobox"
        aria-label={label ?? placeholder}
        aria-haspopup="listbox"
        aria-expanded={open}
        // The listbox exists only while open; never point at a missing id.
        aria-controls={open ? menuId : undefined}
        aria-activedescendant={activeDescendantId}
        disabled={disabled || (options.length === 0 && !searching)}
        onClick={(event) => {
          // WebKit does not focus buttons on pointer clicks by default. Keep
          // arrow-key and Enter handling on the trigger after opening the menu.
          event.currentTarget.focus();
          toggle();
        }}
        onKeyDown={onTriggerKeyDown}
      >
        <span>{triggerLabel}</span>
        <span className="term-select-arrow" aria-hidden="true">
          <svg viewBox="0 0 20 20" focusable="false">
            <path d={open ? "M5 12l5-5 5 5" : "M5 8l5 5 5-5"} />
          </svg>
        </span>
      </button>
      {open && search ? (
        <div className="term-select-menu has-search" style={menuStyle}>
          <div className="term-select-search">
            <Icon name="search" className="search-icon" />
            <input
              ref={searchRef}
              id={search.id}
              className="field-input search-input control-sm"
              type="text"
              role="combobox"
              aria-label={search.label}
              aria-autocomplete="list"
              aria-expanded
              aria-controls={menuId}
              aria-activedescendant={activeDescendantId}
              autoComplete="off"
              spellCheck={false}
              placeholder={search.placeholder}
              value={search.value}
              onChange={(event) => search.onChange(event.target.value)}
              onKeyDown={onSearchKeyDown}
            />
          </div>
          <div id={menuId} className="term-select-list" role="listbox" aria-label={label ?? placeholder}>
            {optionList}
          </div>
          {options.length === 0 ? <p className="term-select-empty" role="status">{search.emptyText}</p> : null}
        </div>
      ) : open ? (
        <div id={menuId} className="term-select-menu" role="listbox" style={menuStyle}>
          {optionList}
        </div>
      ) : null}
    </div>
  );
}
