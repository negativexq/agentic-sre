import { createContext, useContext } from "react";
import type { CanvasFocus, CanvasSelection } from "./selection";
export const InvestigationContext = createContext<{
  focus: CanvasFocus;
  expanded: boolean;
  selection: CanvasSelection;
  select: (selection: CanvasSelection) => void;
} | null>(null);
export const useInvestigationCanvas = () => useContext(InvestigationContext);
