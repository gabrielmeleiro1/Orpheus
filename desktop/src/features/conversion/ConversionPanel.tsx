import type { UseConversionPanelStateArgs } from "./useConversionPanelState";
import { useConversionPanelState } from "./useConversionPanelState";
import { ConversionPanelView } from "./ConversionPanelView";
import "./conversion.css";

type ConversionPanelProps = UseConversionPanelStateArgs;

export function ConversionPanel(props: ConversionPanelProps) {
  const viewModel = useConversionPanelState(props);
  return <ConversionPanelView {...viewModel} />;
}
