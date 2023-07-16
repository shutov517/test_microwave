from datetime import datetime
from functools import wraps
from typing import Optional, Literal

import aioredis
import jwt
import uvicorn
from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import FastAPI
from fastapi.security import HTTPBearer
from pydantic import BaseModel, computed_field
from starlette.datastructures import State

from .settings import redis_config, app_config
from .ws import ws_manager

app: FastAPI = FastAPI()
app.state = State()  # same as in init but IDE highlights
security = HTTPBearer()


def get_redis_connection():
    return aioredis.from_url(
        url=f'redis://:{redis_config.password}@{redis_config.host}:{redis_config.port}/{redis_config.db}'
    )


@app.on_event("startup")
async def startup_event():
    app.state.redis = get_redis_connection()


@app.on_event("shutdown")
async def shutdown_event():
    await app.state.redis.close()


class MicrowaveState(BaseModel):
    power: int = 0
    counter: int = 0

    @computed_field
    @property
    def state(self) -> str:
        return 'ON' if self.counter or self.power else 'OFF'


async def get_remaining_time():
    counter_info = await app.state.redis.hgetall("counter")
    if not counter_info:
        return 0
    counter_value = int(counter_info.get(b"value") or 0)
    counter_timestamp = float(counter_info.get(b"timestamp") or 0)
    elapsed_time = int(datetime.now().timestamp() - counter_timestamp)
    return max(counter_value - elapsed_time, 0)


@app.get("/microwave")
async def get_microwave_state():
    return MicrowaveState(
        power=int(await app.state.redis.get("power") or 0),
        counter=await get_remaining_time(),
    )


def lock_and_return_state_decorator(fn):
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        """
        in the endpoints that change the power and timer, we have logic that requires comparisons (new value >= 0),
        so you can't just do atomic "decr" operation and need a Lock.

        If we will have more than 2 params we can split locks to make 1 lock for 1 param.
            OR we can write function in redis
        Also in this case we can do optimization to send only changed params.
        """
        async with app.state.redis.lock(redis_config.lock_mw_name, timeout=10):
            result = await fn(*args, **kwargs)
            if result:
                return result
            state = await get_microwave_state()
            json_state = state.model_dump_json()
            await ws_manager.broadcast(json_state)
            return state
    return wrapper


@app.websocket("/ws/microwave")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)

    async with app.state.redis.lock(redis_config.lock_mw_name, timeout=10):
        state = await get_microwave_state()
        await ws_manager.send_personal_message(state.model_dump_json(), websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.post("/microwave/power/increase")
@lock_and_return_state_decorator
async def increase_power():
    await app.state.redis.incr("power", amount=10)


@app.post("/microwave/power/decrease")
@lock_and_return_state_decorator
async def decrease_power():
    power = int(await app.state.redis.get("power") or 0)
    power = max(power - 10, 0)
    await app.state.redis.set("power", power)


@app.post("/microwave/counter/increase")
@lock_and_return_state_decorator
async def increase_counter():
    remaining_time = await get_remaining_time() + 10
    await app.state.redis.hmset("counter", {"value": remaining_time, "timestamp": datetime.now().timestamp()})


@app.post("/microwave/counter/decrease")
@lock_and_return_state_decorator
async def decrease_counter():
    remaining_time = max(await get_remaining_time() - 10, 0)
    await app.state.redis.hmset("counter", {"value": remaining_time, "timestamp": datetime.now().timestamp()})


@app.post("/microwave/cancel")
@lock_and_return_state_decorator
async def cancel_microwave(token: Optional[str] = Depends(security)):
    try:
        payload = jwt.decode(token.credentials, app_config.secret_key, algorithms=[app_config.crypto_algorithm])
        if payload["valid"]:
            await app.state.redis.set("power", 0)
            await app.state.redis.hmset("counter", {"value": 0, "timestamp": datetime.now().timestamp()})
            return
    except jwt.DecodeError:
        pass
    raise HTTPException(status_code=401, detail="Invalid token")


def generate_token():
    payload = {"valid": True}
    token = jwt.encode(payload, app_config.secret_key, algorithm=app_config.crypto_algorithm)
    return token


@app.post("/microwave/login")
def login():
    # TODO: users & login
    return {"token": generate_token()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
