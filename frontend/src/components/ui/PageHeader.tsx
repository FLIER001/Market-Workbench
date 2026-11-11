import { type ReactNode } from "react";
import { createPortal } from "react-dom";

interface Props {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}

export function PageHeader({ actions }: Props) {
  const target = document.getElementById("page-actions");
  return actions && target ? createPortal(<div className="flex items-center gap-2">{actions}</div>, target) : null;
}
