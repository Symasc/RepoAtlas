import { proxy } from "@/lib/server/proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Only POST is exported: Next answers every other method with 405. */
export async function POST(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  return proxy(request, path.join("/"));
}
