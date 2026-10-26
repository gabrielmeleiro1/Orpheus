import { useId } from "react";

import { Icon } from "../../../ui/icons";

interface ConversionHelpProps {
  label: string;
  text: string;
}

export function ConversionHelp({ label, text }: ConversionHelpProps) {
  const descriptionId = useId();
  return (
    <>
      <button
        className="btn btn-ghost btn-icon btn-info"
        type="button"
        aria-label={label}
        aria-describedby={descriptionId}
        data-tip={text}
        data-tip-pos="right"
      >
        <Icon name="info" />
      </button>
      <span id={descriptionId} className="sr-only">{text}</span>
    </>
  );
}
