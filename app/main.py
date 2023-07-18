from datetime import datetime
from functools import wraps
from typing import Optional

import aioredis
import jwt
import uvicorn
from fastapi import Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi import FastAPI
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse
from pydantic import BaseModel, computed_field
from starlette.datastructures import State
from passlib.context import CryptContext

from .settings import redis_config, app_config
from .ws import ws_manager

app: FastAPI = FastAPI()
app.state = State()  # same as in init but IDE highlights
security = OAuth2PasswordBearer(tokenUrl="microwave/login")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# This is a basic dictionary for authentication.
# Replace this with a proper database or authentication backend
USER_DATA = {
    "testuser": pwd_context.hash("testpassword")
}


def authenticate_user(username: str, password: str):
    if username in USER_DATA and pwd_context.verify(password, USER_DATA[username]):
        return username
    else:
        return None


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


@app.get("/microwave", response_model=MicrowaveState)
async def get_microwave_state():
    """
    Get the current microwave state
    """
    return MicrowaveState(
        power=int(await app.state.redis.get("power") or 0),
        counter=await get_remaining_time(),
    )


def lock_and_return_state_decorator(fn):
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        async with app.state.redis.lock(redis_config.lock_mw_name, timeout=10):
            result = await fn(*args, **kwargs)
            if result:
                return result
            state = await get_microwave_state()
            json_state = state.model_dump()
            await ws_manager.broadcast(json_state)
            return json_state
    return wrapper


@app.websocket("/ws/microwave")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint to get microwave updates
    """
    await ws_manager.connect(websocket)

    async with app.state.redis.lock(redis_config.lock_mw_name, timeout=10):
        state = await get_microwave_state()
        await ws_manager.send_personal_message(state.model_dump_json(), websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.post("/microwave/power/increase",
          response_model=MicrowaveState,
          responses={200: {'description': 'Increase microwave power by 10'}})
@lock_and_return_state_decorator
async def increase_power():
    """
    Increase microwave power by 10
    """
    await app.state.redis.incr("power", amount=10)


@app.post("/microwave/power/decrease",
          response_model=MicrowaveState,
          responses={200: {'description': 'Decrease microwave power by 10'}})
@lock_and_return_state_decorator
async def decrease_power():
    """
    Decrease microwave power by 10
    """
    power = int(await app.state.redis.get("power") or 0)
    power = max(power - 10, 0)
    await app.state.redis.set("power", power)


@app.post("/microwave/counter/increase",
          response_model=MicrowaveState,
          responses={200: {'description': 'Increase microwave counter by 10'}})
@lock_and_return_state_decorator
async def increase_counter():
    """
    Increase microwave counter by 10
    """
    remaining_time = await get_remaining_time() + 10
    await app.state.redis.hmset("counter", {"value": remaining_time, "timestamp": datetime.now().timestamp()})


@app.post("/microwave/counter/decrease",
          response_model=MicrowaveState,
          responses={200: {'description': 'Decrease microwave counter by 10'}})
@lock_and_return_state_decorator
async def decrease_counter():
    """
    Decrease microwave counter by 10
    """
    remaining_time = max(await get_remaining_time() - 10, 0)
    await app.state.redis.hmset("counter", {"value": remaining_time, "timestamp": datetime.now().timestamp()})


@app.post("/microwave/cancel",
          response_model=MicrowaveState,
          responses={200: {'description': 'Decrease microwave counter by 10'}})
@lock_and_return_state_decorator
async def cancel_microwave(token: Optional[str] = Depends(security)):
    """
    Cancel microwave operations (set all to 0)
    """
    try:
        payload = jwt.decode(token, app_config.secret_key, algorithms=[app_config.crypto_algorithm])
        if payload["valid"]:
            await app.state.redis.set("power", 0)
            await app.state.redis.hmset("counter", {"value": 0, "timestamp": datetime.now().timestamp()})
            return
    except jwt.DecodeError:
        pass
    raise HTTPException(status_code=401, detail="Invalid token")


def generate_token(username: str):
    payload = {"username": username, "valid": True}
    token = jwt.encode(payload, app_config.secret_key, algorithm=app_config.crypto_algorithm)
    return token


@app.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Login endpoint to authenticate users
    """
    username = authenticate_user(form_data.username, form_data.password)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"access_token": generate_token(username), "token_type": "bearer"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
