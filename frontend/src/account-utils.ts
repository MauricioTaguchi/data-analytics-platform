import axios from "axios";

export type AccountMode = "login" | "register" | "forgot" | "reset";

export function passwordValidationMessage(password: string, confirmation: string) {
  if ([...password].length < 10 || !/\p{L}/u.test(password) || !/\p{N}/u.test(password)) return "Use at least 10 characters, including a letter and a number.";
  if (new TextEncoder().encode(password).length > 72) return "This password is too long. Try fewer characters, especially symbols or emoji.";
  if (password !== confirmation) return "The passwords do not match. Enter the same password in both fields.";
  return null;
}

export function accountErrorMessage(error: unknown, mode: AccountMode | "account") {
  if (!axios.isAxiosError(error)) return "We couldn't complete your request. Please try again.";
  const status = error.response?.status;
  if (status === 429) return "Too many attempts. Please wait a few minutes before trying again.";
  if (mode === "reset" && status === 400) return "This reset link is invalid or has expired. Request a new link below.";
  if (mode === "login" && status === 401) return "Email or password is incorrect. Check both and try again.";
  if (mode === "register" && status === 409) return "This email is already registered. Sign in or reset your password.";
  if (status === 422) return mode === "forgot" ? "Enter a valid email address and try again." : "Check your details. A new password needs at least 10 characters, a letter, and a number.";
  if (!error.response) return "We couldn't reach the service. Check your connection and try again.";
  return "The service couldn't complete your request. Please try again shortly.";
}

export function consumePasswordResetToken(location: Pick<Location, "href">, history: Pick<History, "replaceState" | "state">) {
  const url = new URL(location.href);
  const fragment = new URLSearchParams(url.hash.slice(1));
  if (!fragment.has("reset_token")) return null;
  const token = fragment.get("reset_token") || "";
  fragment.delete("reset_token");
  url.hash = fragment.toString();
  history.replaceState(history.state, "", `${url.pathname}${url.search}${url.hash}`);
  return token.length <= 2048 ? token : "";
}
