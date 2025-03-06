// functions/_middleware.js
export async function onRequest(context) {
    const request = context.request;
    
    // Handle CORS preflight requests
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 200,
        headers: {
          "Access-Control-Allow-Origin": "https://smith-frontend.vercel.app",
          "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Session-ID, X-Request-ID, X-Cancel-Previous",
          "Access-Control-Allow-Credentials": "true",
          "Access-Control-Max-Age": "86400",
        },
      });
    }
    
    // Forward the request to the next handler (your FastAPI app or route handler)
    const response = await context.next();
    
    // Clone the response so we can modify the headers
    const newResponse = new Response(response.body, response);
    
    // Add CORS headers to all responses
    newResponse.headers.set("Access-Control-Allow-Origin", "https://smith-frontend.vercel.app");
    newResponse.headers.set("Access-Control-Allow-Credentials", "true");
    
    return newResponse;
  }