"""Quick end-to-end smoke test: run the proxy against a fake upstream."""
import httpx
import uvicorn

from proxy.main import app


async def run():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        })

    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream.test/v1"
    )
    # The lifespan creates its own client at server startup; overwrite it with
    # the mock one AFTER startup so requests use the mock upstream.
    config = uvicorn.Config(app, host="127.0.0.1", port=8123, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream.test/v1"
    )

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8123") as c:
        r = await c.post("/v1/chat/completions",
                         headers={"Authorization": "Bearer k"},
                         json={"model": "gpt-4o-mini",
                               "messages": [{"role": "user", "content": "hello there"}]})
        print("chat:", r.status_code, r.json()["choices"][0]["message"]["content"])
        r = await c.get("/stats?format=text")
        print(r.text)
        r = await c.get("/health")
        print("health:", r.json())

    server.should_exit = True
    await server_task
    await app.state.http.aclose()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
