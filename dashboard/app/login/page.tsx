import { LoginForm } from "./LoginForm";

export const metadata = { title: "Sign in · Hedj" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const { next = "/" } = await searchParams;
  return (
    <main className="mx-auto flex min-h-[80vh] w-full max-w-xs flex-col justify-center gap-6 px-4">
      <div>
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted">Hedj</p>
        <h1 className="mt-1 text-2xl font-semibold">Trading desk</h1>
      </div>
      <LoginForm next={next} />
    </main>
  );
}
