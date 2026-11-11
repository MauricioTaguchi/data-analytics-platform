import axios from "axios";
import { apiClient, describeApiError, requirePositiveId } from "./api-client";

export type DeletionTarget =
  | { scope: "dataset" | "project"; id: number; name: string }
  | { scope: "account"; name: string };

export type DeletionReceipt = {
  status: "deleted";
  scope: "dataset" | "project" | "account";
  cleanup_pending: boolean;
  message: string;
};

export async function deleteSavedResource(target: DeletionTarget, confirmation: string, password = "") {
  if (confirmation !== "DELETE") throw new Error("Type DELETE exactly to confirm.");
  if (target.scope === "account" && !password) throw new Error("Enter your current password.");
  const path = target.scope === "account" ? "/lifecycle/account"
    : `/lifecycle/${target.scope === "dataset" ? "datasets" : "projects"}/${requirePositiveId(target.id)}`;
  const data = target.scope === "account" ? { confirmation, current_password: password } : { confirmation };
  return (await apiClient.delete<DeletionReceipt>(path, { data })).data;
}

export function deletionErrorMessage(error: unknown, scope: DeletionTarget["scope"]) {
  if (axios.isAxiosError(error)) {
    const status = error.response?.status;
    if (status === 409) return "Wait for active jobs to finish or cancel them in Operations monitoring before deleting. Cancellation must finish before you retry.";
    if (status === 403 && scope === "account") return "Your current password was not accepted. Check it and try again.";
    if (status === 404) return "This item is no longer available. Close this dialog and refresh your workspace.";
    if (!status || status >= 500) return "The service could not confirm the result. The deletion may have been accepted. Close this dialog and refresh your workspace before retrying; for an account, check whether you can still sign in.";
  }
  return describeApiError(error);
}
