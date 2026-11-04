import type { KeyboardEvent } from "react";

/** Enter submits the field's form, the same as clicking its submit button. Shift+Enter keeps the
 * default (a newline in a multi-line field); nothing is submitted while an IME composition is active
 * (`isComposing`, or the legacy keyCode 229 some browsers report for the composing Enter). */
export function submitOnEnter(event: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>): void {
  if (event.key !== "Enter" || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
  const native = event.nativeEvent as globalThis.KeyboardEvent;
  if (native.isComposing || native.keyCode === 229) return;
  const form = event.currentTarget.form;
  if (!form) return;
  event.preventDefault();
  form.requestSubmit();
}
