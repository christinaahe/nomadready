"""NomadReady: FastAPI app. POST /chat keeps Project 1's shape:
    request  {"message": str, "session_id": str | null}
    response {"response": str, "session_id": str, "tool_calls": [{"agent", "name", "args", "result"}]}
POST /run takes the same request and streams progress as server-sent events (the page uses it).
"""

import asyncio
import json
import threading
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")  # GOOGLE_PLACES_API_KEY; before the tools read the environment

from scout.intake import _calendar_window  # noqa: E402
from scout.pipeline import answer  # noqa: E402
from scout.writer import cited_ids  # noqa: E402
from tools.db import rows  # noqa: E402

# session_id -> the conversation ({role, content}). In-memory, single process.
sessions: dict[str, list[dict]] = {}
busy: set[str] = set()  # sessions with a question in progress

app = FastAPI(title="NomadReady")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


BUSY = "I'm still working on your last question in this conversation. Wait for it to finish, or start a new chat."


def _failed(e: Exception) -> str:
    # Auth, quota, a model that won't stop: show it in the chat, not as a 500.
    return f"Something went wrong: {type(e).__name__}: {str(e)[:300]}"


@app.get("/about")
def about():
    """What the page needs to describe the data: covered cities, snapshot dates, the bookable window."""
    first, last = _calendar_window()
    cities = rows("select city, country, airbnb_snapshot_date, ookla_quarter from cities order by city")
    return {
        "cities": [{"city": c["city"], "country": c["country"]} for c in cities],
        "airbnb_snapshots": [min(c["airbnb_snapshot_date"] for c in cities).isoformat(),
                             max(c["airbnb_snapshot_date"] for c in cities).isoformat()],
        "ookla_quarter": cities[0]["ookla_quarter"],
        "window": [first.isoformat(), last.isoformat()],
        "min_nights": rows("select min_stay_nights from build_info")[0]["min_stay_nights"],
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    if session_id in busy:
        return ChatResponse(response=BUSY, session_id=session_id, tool_calls=[])
    conversation = sessions.setdefault(session_id, [])
    conversation.append({"role": "user", "content": request.message})
    busy.add(session_id)
    try:
        response, state = await answer(conversation)
        tool_calls = state.tool_calls
    except Exception as e:
        response, tool_calls = _failed(e), []
    finally:
        busy.discard(session_id)
    conversation.append({"role": "assistant", "content": response})
    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


@app.post("/run")
async def run(request: ChatRequest):
    """/chat with live progress, as server-sent events. Same request body; same session as /chat.
    Events, in order:
        session                       {session_id}
        phase / agent / step / tool_call / brief / checked   while it works (see scout/state.py)
        done                          {response, session_id, clarifying, n_tool_calls, t}
        error                         {message}  instead of done, if something failed
    A ": ping" comment every 15 s keeps proxies from closing a quiet connection. If the page
    disconnects, the run is cancelled and the question is dropped from the conversation."""
    session_id = request.session_id or str(uuid.uuid4())

    async def events():
        yield _sse({"type": "session", "session_id": session_id})
        if session_id in busy:
            yield _sse({"type": "error", "message": BUSY})
            return
        conversation = sessions.setdefault(session_id, [])
        conversation.append({"role": "user", "content": request.message})
        busy.add(session_id)
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        queue: asyncio.Queue[dict] = asyncio.Queue()

        def emit(event: dict) -> None:
            # Hooks run on the event loop, so events are queued in order, before the run can finish.
            if threading.get_ident() == loop_thread:
                queue.put_nowait(event)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, event)

        task = asyncio.create_task(answer(conversation, emit=emit))
        finished = False
        try:
            while not (task.done() and queue.empty()):
                try:
                    yield _sse(await asyncio.wait_for(queue.get(), timeout=15))
                except TimeoutError:
                    if not task.done():
                        yield ": ping\n\n"
            try:
                response, state = task.result()
                conversation.append({"role": "assistant", "content": response})
                finished = True
                sources = [{"id": cid, **state.citations[cid]} for cid in cited_ids(response)
                           if cid in state.citations]
                yield _sse({"type": "done", "response": response, "session_id": session_id,
                            "clarifying": not state.brief, "n_tool_calls": len(state.tool_calls),
                            "sources": sources, "t": round(state.elapsed(), 1)})
            except Exception as e:
                response = _failed(e)
                conversation.append({"role": "assistant", "content": response})
                finished = True
                yield _sse({"type": "error", "message": response})
        finally:
            busy.discard(session_id)
            if not finished:  # the page went away mid-run: stop spending on it, forget the question
                task.cancel()
                if conversation and conversation[-1] == {"role": "user", "content": request.message}:
                    conversation.pop()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
