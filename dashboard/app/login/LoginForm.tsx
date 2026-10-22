"use client";

import { useActionState } from "react";
import { login, type LoginState } from "./actions";

export function LoginForm({ next }: { next: string }) {
  const [state, action, pending] = useActionState<LoginState, FormData>(login, {});
  return (
    <form action={action} className="flex flex-col gap-3">
      <input type="hidden" name="next" value={next} />
      <label htmlFor="password" className="text-sm text-muted">Password</label>
      <input
        id="password"
        name="password"
        type="password"
        autoComplete="current-password"
        autoFocus
        required
        className="rounded-md border border-line bg-panel px-3 py-2 text-base outline-none focus:border-fg"
      />
      {state.error && <p role="alert" className="text-sm text-down">{state.error}</p>}
      <button
        type="submit"
        disabled={pending}
        className="mt-1 rounded-md bg-fg px-3 py-2 text-sm font-medium text-bg transition-opacity disabled:opacity-60"
      >
        {pending ? "Checking…" : "Sign in"}
      </button>
    </form>
  );
}
