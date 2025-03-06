from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from llm import generate_response, generate_related_questions
import json
import logging
import os
from dotenv import load_dotenv
from uuid import uuid4
import time
from starlette.background import BackgroundTask
# Import the React Agent
from react_agent.graph import graph
from react_agent.configuration import Configuration
from langchain_core.messages import HumanMessage, AIMessage
import asyncio

# Load environment variables
load_dotenv()

# --- ENVIRONMENT VARIABLE CHECKS ---
required_env_vars = [
    'GOOGLE_API_KEY',
    'OPENAI_API_KEY',
    'ANTHROPIC_API_KEY',
    'FIREWORKS_API_KEY',
    'GROQ_API_KEY'  #  Make sure this is set if you use Groq
]

missing_vars = [var for var in required_env_vars if not os.getenv(var)]

if missing_vars:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(missing_vars)}. "
        "Please ensure these are set in your deployment environment."
    )

# --- END ENVIRONMENT VARIABLE CHECKS ---

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure CORS - updated to be more permissive for debugging
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for testing
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
    expose_headers=["*"],
)

# Add session management
sessions = {}

async def get_session_id(request: Request):
    """Get or create a session ID from request headers."""
    session_id = request.headers.get('X-Session-ID')
    if not session_id:
        session_id = f"session_{uuid4()}"
    
    # Initialize session if it doesn't exist
    if session_id not in sessions:
        sessions[session_id] = {
            'created_at': time.time(),
            'last_accessed': time.time()
        }
    else:
        sessions[session_id]['last_accessed'] = time.time()
    
    return session_id

# Add CORS options endpoint for preflight requests
@app.options("/{rest_of_path:path}")
async def options_route(rest_of_path: str):
    return JSONResponse(
        content={"message": "OK"},
        headers={
            "Access-Control-Allow-Origin": "https://smith-frontend.vercel.app",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Max-Age": "86400",
        }
    )

@app.post("/chat")
async def chat_endpoint(request: Request, session_id: str = Depends(get_session_id)):
    try:
        # Cancel previous request if header is present
        if request.headers.get('X-Cancel-Previous') == 'true':
            previous_request = sessions[session_id].get('current_request')
            if previous_request:
                sessions[session_id]['cancelled'] = True

        body = await request.json()
        message = body.get('message', '').strip()
        model = body.get('model', 'gemini-1.5-flash').strip()  # Use full model ID directly

        request_id = request.headers.get('X-Request-ID')
        sessions[session_id]['current_request'] = request_id
        sessions[session_id]['cancelled'] = False

        if not message:
            raise HTTPException(status_code=400, detail="No message provided")

        messages = [{"role": "user", "content": message}]
        async def generate():
            async for text in generate_response(messages, model, session_id):
                if sessions[session_id].get('cancelled', False):
                    break
                yield f"data: {text}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(), 
            media_type="text/event-stream",
            headers={
                "Access-Control-Allow-Origin": "https://smith-frontend.vercel.app",
                "Access-Control-Allow-Credentials": "true",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/event-stream",
                "X-Accel-Buffering": "no"
            }
        )
    except Exception as e:
        logger.error(f"Chat endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/agent-chat")
async def agent_chat_endpoint(request: Request, session_id: str = Depends(get_session_id)):
    try:
        # Update session last accessed time
        sessions[session_id]['last_accessed'] = time.time()
        
        body = await request.json()
        message = body.get('message', '').strip()
        model = body.get('model', 'gemini-1.5-flash').strip()
        request_id = request.headers.get('X-Request-ID')

        sessions[session_id]['current_request'] = request_id
        sessions[session_id]['cancelled'] = False
        
        if not message:
            raise HTTPException(status_code=400, detail="No message provided")
        
        logger.info(f"Agent chat request from session {session_id}: {message[:50]}...")
        
        # Stream the response from React Agent
        async def generate():
            try:
                # Configure the agent - ADD PREFIX to model
                config = {
                    "configurable": {
                        "model": f"custom/{model}",  # Add the 'custom/' prefix
                        "max_search_results": 5
                    }
                }

                # Set up input for the agent
                input_state = {"messages": [HumanMessage(content=message)]}
                
                # Get the agent response as a stream
                streaming_response = ""
                
                try:
                    # Create a task to run the agent
                    agent_task = asyncio.create_task(graph.ainvoke(input_state, config))
                    
                    # Initialize a flag to track if we're done
                    is_done = False
                    
                    # Stream intermediate updates from the agent
                    while not is_done:
                        # Check if request has been cancelled
                        if sessions[session_id].get('cancelled', False):
                            agent_task.cancel()
                            break
                        
                        # Get current state if task is done
                        if agent_task.done():
                            try:
                                result = agent_task.result()
                                # Get the final AI message
                                for msg in reversed(result["messages"]):
                                    if isinstance(msg, AIMessage) and not msg.tool_calls:
                                        streaming_response = msg.content
                                        break
                                is_done = True
                            except Exception as e:
                                logger.error(f"Error getting agent result: {str(e)}")
                                streaming_response = f"Error: {str(e)}"
                                is_done = True
                        
                        # Format as SSE and yield
                        response_json = json.dumps({"response": streaming_response})
                        yield f"data: {response_json}\n\n"
                        
                        # Short pause before next check
                        if not is_done:
                            await asyncio.sleep(0.1)
                except Exception as e:
                    logger.error(f"Agent execution error: {str(e)}")
                    streaming_response = f"Error with agent: {str(e)}"
                    response_json = json.dumps({"response": streaming_response})
                    yield f"data: {response_json}\n\n"
                
                # Send final DONE message
                yield "data: [DONE]\n\n"
                
            except Exception as e:
                logger.error(f"Error in agent generate: {str(e)}")
                error_json = json.dumps({"error": str(e)})
                yield f"data: {error_json}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Access-Control-Allow-Origin": "https://smith-frontend.vercel.app",
                "Access-Control-Allow-Credentials": "true",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/event-stream",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:
        logger.error(f"Agent chat endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/related-questions")
async def related_questions_endpoint(request: Request):
    try:
        body = await request.json()
        message = body.get('message', '').strip()
        model = body.get('model', 'gemini-pro').strip()

        if not message:
            raise HTTPException(status_code=400, detail="No message provided")

        # Generate related questions
        questions = await generate_related_questions(message, model)

        return JSONResponse(
            content={
                "success": True,
                "questions": questions
            },
            headers={
                "Access-Control-Allow-Origin": "https://smith-frontend.vercel.app",
                "Access-Control-Allow-Credentials": "true",
            }
        )

    except Exception as e:
        logger.error(f"Related questions error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Health check endpoint
@app.get("/health")
async def health_check():
    return JSONResponse(
        content={"status": "healthy"},
        headers={
            "Access-Control-Allow-Origin": "https://smith-frontend.vercel.app",
            "Access-Control-Allow-Credentials": "true",
        }
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)