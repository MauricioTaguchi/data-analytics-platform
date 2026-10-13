import { useEffect, useRef } from "react";

const focusableSelector = "button, a[href], input, select, textarea, [tabindex], [contenteditable='true']";

function currentFocus(): HTMLElement | null {
  return typeof document !== "undefined" && document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
}

function focusableElements(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector)).filter((element) => (
    element.tabIndex >= 0
    && !element.matches(":disabled")
    && !element.closest("[hidden], [inert]")
    && element.getClientRects().length > 0
    && getComputedStyle(element).visibility === "visible"
  ));
}

export function useDialogFocus(open: boolean, onClose: () => void) {
  const dialogRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  const previousFocusRef = useRef(currentFocus());

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) {
      const rememberFocus = () => {
        const active = currentFocus();
        if (!active?.closest('[role="dialog"], [role="alertdialog"]')) previousFocusRef.current = active;
      };
      rememberFocus();
      document.addEventListener("focusin", rememberFocus);
      return () => document.removeEventListener("focusin", rememberFocus);
    }

    const dialog = dialogRef.current;
    if (!dialog) return;
    const activeElement = currentFocus();
    const previousFocus = activeElement && !dialog.contains(activeElement)
      ? activeElement
      : previousFocusRef.current;

    if (!activeElement || !dialog.contains(activeElement)) {
      const elements = focusableElements(dialog);
      const initialFocus = elements.find((element) => element.dataset.dialogInitialFocus === "true")
        ?? elements.find((element) => element.matches("input, select, textarea, [contenteditable='true']"));
      (initialFocus ?? elements[0] ?? dialog).focus();
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const elements = focusableElements(dialog);
      const first = elements[0];
      const last = elements[elements.length - 1];
      const active = currentFocus();
      if (!first || !last) {
        event.preventDefault();
        dialog.focus();
      } else if (!active || !dialog.contains(active) || active === dialog) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [open]);

  return dialogRef;
}
