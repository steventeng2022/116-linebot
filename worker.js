const ORIGIN = "https://lineb.172-235-214-249.sslip.io";

export default {
  async fetch(request) {
    const incomingUrl = new URL(request.url);
    if (incomingUrl.pathname === "/") {
      return Response.redirect(`${incomingUrl.origin}/admin`, 302);
    }
    const upstreamUrl = new URL(incomingUrl.pathname + incomingUrl.search, ORIGIN);
    const headers = new Headers(request.headers);

    headers.delete("host");
    headers.set("x-forwarded-host", incomingUrl.host);

    const upstreamResponse = await fetch(
      new Request(upstreamUrl, {
        method: request.method,
        headers,
        body: request.body,
        redirect: "manual",
      }),
    );

    const responseHeaders = new Headers(upstreamResponse.headers);
    const location = responseHeaders.get("location");
    if (location?.startsWith(ORIGIN)) {
      responseHeaders.set("location", location.replace(ORIGIN, incomingUrl.origin));
    }

    return new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      statusText: upstreamResponse.statusText,
      headers: responseHeaders,
    });
  },
};
